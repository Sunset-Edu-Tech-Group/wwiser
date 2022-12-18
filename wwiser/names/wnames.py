import logging, re, os, os.path, sys
from datetime import datetime

from .. import wfnv
from ..parser import wdefs
from .wsqlite import SqliteHandler
from .wnamerow import NameRow
from . import wnconfig


# Parses various companion files with names and saves results, later used to assign names to bank's
# ShortIDs. Resulting name list may be either ID=HASHNAME, where ID is a hash of HASHNAME (events,
# game syncs, soundsbanks, possibly others), or ID=GUIDNAME where it'a hash of the GUID (other objects).
# No need to know intended target (event vs wem) since names can be tested to be hashname or not.
#
# For hashnames, Wwise enforces that names must be unique in the same proyect, but the hashing
# algorithm is simple and prone to collisions. When parsing companion files, game's files should
# be loaded before generic (wwnames.db3) name lists, to minimize the chance to load wrong names.
# Hashnames are case insensitive, but SoundbanksInfo.xml may have Play_Thing while Wwise_IDs.h
# hash PLAY_THING. This code doesn't normalize names so priority is given to the former.
# GUIDNAMEs may be given multiple variations in different files too.
#
# IDs may also have an "object path" (logical) or "path" (physical), that are never a HASHNAMEs,
# but give extra hints.
#
# Companion files are created by the editor depending on the "project settings" options.
#******************************************************************************

class Names(object):
    ONREPEAT_INCLUDE = 1
    ONREPEAT_IGNORE = 2
    ONREPEAT_BEST = 3
    EMPTY_BANKTYPE = ''


    def __init__(self):
        #self._gamename = None #info txt
        self._bankname = None
        self._names = {}
        self._names_fuzzy = {}
        self._db = None
        self._loaded_wwnames = {}
        self._loaded_banknames = set()
        self._missing = {}
        self._fnv = wfnv.Fnv()
        # flags
        self._cfg = wnconfig.Config()

    def set_gamename(self, gamename):
        self._gamename = gamename #path

    def _mark_used(self, row, hashtype, node):
        #if row.hashname_used:
        #    return
        row.hashname_used = True

        if self._cfg.classify and hashtype:
            if not row.hashtypes:
                row.hashtypes = set()
            
            # even if marked sometimes 
            if self._cfg.classify_bank:
                # should only happen when reading GV/SC/GS params, ignore
                if not node:
                    return

                bank = node.get_root().get_filename()
                # put all bnk together
                if hashtype in wdefs.fnv_order_join:
                    bank = self.EMPTY_BANKTYPE
            else:
                bank = self.EMPTY_BANKTYPE

            key = (hashtype, bank)
            row.hashtypes.add(key)
            self._loaded_banknames.add(bank)

        # log this first time it's marked as used
        if not row.multiple_marked and row.hashnames:
            old = row.hashname
            new = row.hashnames[0]
            # could show more but not too interesting
            logging.info("names: alt hashnames (using old), old=%s vs new=%s" % (old, new))
            row.multiple_marked = True

    def _mark_unused(self, id, hashtype, node):

        if self._cfg.classify_bank:
            # should only happen when reading GV/SC/GS params, ignore
            if not node:
                return

            bank = node.get_root().get_filename()
            # put all bnk together
            if hashtype in wdefs.fnv_order_join:
                bank = self.EMPTY_BANKTYPE
        else:
            bank = self.EMPTY_BANKTYPE

        banks = self._missing.get(hashtype)
        if not banks:
            banks = {}
            self._missing[hashtype] = banks

        ids = banks.get(bank)
        if not ids:
            ids = {}
            banks[bank] = ids

        ids[id] = True

    def _unmark_unused(self, id):
        for hashtype in self._missing.keys():
            for bank in self._missing[hashtype].keys():
                if id in self._missing[hashtype][bank]:
                    del self._missing[hashtype][bank][id]

    def get_namerow(self, id, hashtype=None, node=None):
        if not id or id == -1: #including id=0, that is used as "none"
            return None
        id = int(id)
        no_hash = hashtype == 'none'

        # on list
        row = self._names.get(id)
        if row:
            # in case of guidnames don't mark but allow row
            if not (row.hashname and no_hash):
                self._mark_used(row, hashtype, node)
            return row

        # hashnames not allowed
        if no_hash:
            # next tests only find ids with hashnames
            return None

        # on list with a close ID
        row_fz = None
        if not self._cfg.disable_fuzzy:
            id_fz = id & 0xFFFFFF00
            row_fz = self._names_fuzzy.get(id_fz)
        if row_fz and row_fz.hashname:
            hashname_uf = self._fnv.unfuzzy_hashname(id, row_fz.hashname)
            row = self._add_name(id, hashname_uf, source=NameRow.NAME_SOURCE_EXTRA)
            if row:
                self._mark_used(row, hashtype, node)
                return row

        if not self._db:
            return None

        # on db (add to names for easier access and saving list of wwnames)
        # when using db always set extended hash to allow bus names (and maybe guidnames?)
        row_db = self._db.select_by_id(id)
        if row_db:
            row = self._add_name(id, row_db.hashname, source=NameRow.NAME_SOURCE_EXTRA, exhash=True)
            if row:
                self._mark_used(row, hashtype, node)
                return row

        # on db with a close ID
        row_df = None
        if not self._cfg.disable_fuzzy:
            row_df = self._db.select_by_id_fuzzy(id)
        if row_df and row_df.hashname:
            hashname_uf = self._fnv.unfuzzy_hashname(id, row_df.hashname)
            row = self._add_name(id, hashname_uf, source=NameRow.NAME_SOURCE_EXTRA, exhash=True)
            if row:
                self._mark_used(row, hashtype, node)
                return row


        # groups missing ids (uninteresting ids like AkSound don't pass type)
        if hashtype:
            self._mark_unused(id, hashtype, node)

        return None

    # IDs come from hashed NAME (32b, where name follows rules) or hashed GUIDs (30b, where NAME is arbitrary),
    # so first we check the type. Sometimes IDs that should come from GUID (like BUS names, according to
    # AK's docs) are actually from NAMEs, so it's worth manually testing rather than trusting the caller.
    # Multiple GUIDNAMEs for an ID are possible, so we can update the results, and we can also add Wwise's
    # "Path/ObjectPath" for extra info (never hashnames, considered separate).
    def _add_name(self, id, name, objpath=None, path=None, onrepeat=ONREPEAT_INCLUDE, exhash=False, source=None):
        if name:
            name = name.strip()
        if objpath:
            objpath = objpath.strip()
        if path:
            path = path.strip()
        if not name: #after strip
            return None

        lowname = name.lower()
        hashable = self._fnv.is_hashable(lowname)
        extended = False
        if not hashable and exhash:
            hashable = self._fnv.is_hashable_extended(lowname)
            extended = hashable

        if not id and not hashable:
            return None
        id_hash = self._fnv.get_hash_lw(lowname)

        if not id:
            id = id_hash
        else:
            id = int(id)
        is_hashname = id == id_hash

        row = self._names.get(id)
        if row:
            #ignore even if guidname
            if onrepeat == self.ONREPEAT_IGNORE:
                return row

            if is_hashname and row.hashname:
                if row.hashname.lower() != name.lower():
                    #logging.info("names: alt hashname (using old), old=%s vs new=%s" % (row.hashname, name))
                    #return None #allow to add as alt, logged once used
                    pass
                # ignore new name if all uppercase (favors lowercase names)
                if onrepeat == self.ONREPEAT_BEST and name.isupper():
                    #logging.info("names: ignoring new uppercase name, new=%s vs old=%s" % (name, row.hashname))
                    return None
                #logging.info("names: updating row, new=%s vs old=%s" % (name, row.hashname))
        else:
            row = NameRow(id)
            row.source = source
            self._names[id] = row

        if is_hashname:
            row.add_hashname(name, extended=extended)
            #logging.info("names: added id=%i, hashname=%s" % (id, name))
        else:
            row.add_guidname(name)
            #logging.info("names: added id=%i, guidname=%s" % (id, name))

        if objpath:
            row.add_objpath(objpath)
            #logging.info("names: added id=%i, objpath=%s" % (id, objpath))

        if path:
            row.add_path(path)
            #logging.info("names: added id=%i, path=%s" % (id, path))

        # reference to get close names
        if row.hashname:
            id_fuzzy = id & 0xFFFFFF00
            self._names_fuzzy[id_fuzzy] = row #latest row is ok

        # in case it was registered
        if is_hashname:
            self._unmark_unused(id)

        return row

    # *************************************************************************

    def parse_files(self, banks, filenames, xml=None, txt=None, h=None, lst=None, db=None, json=None):
        if not filenames:
            return
        logging.info("names: loading names")

        # add banks names (doubles as hashnames), first since it looks a bit nicer in list output
        for bank in banks:
            bankname = bank.get_root().get_bankname()
            self._add_name(None, bankname, source=NameRow.NAME_SOURCE_EXTRA)

        # parse files for each single bank
        for filename in filenames:
            # update current bank name (in case of mixed bank dirs; repeats aren't parsed again)
            self.set_bankname(filename)

            # from more to less common/useful
            self.parse_xml(xml)
            self.parse_xml_bnk(xml)
            self.parse_txt_bnk(txt)
            self.parse_json(json)
            self.parse_json_bnk(json)

        # banks may store some extra hashname strings (rarely)
        for bank in banks:
            strings = bank.get_root().get_strings()
            for string in strings:
                self._add_name(None, string, source=NameRow.NAME_SOURCE_EXTRA)

        # parse .h (names in CAPS so less priority)
        for filename in filenames:
            self.set_bankname(filename)
            self.parse_h(h)

        # extra files, after other banks or priority when generating some missing lists and stuff is off
        for filename in filenames:
            # try wwnames in bnk folder
            self.set_bankname(filename)
            self.parse_lst(lst)

            # also try in prev folder, for easier names in localized dirs
            pathname = os.path.dirname(filename)
            basename = os.path.basename(filename)
            prevname = os.path.join(pathname, '..')
            prevname = os.path.join(prevname, basename)
            self.set_bankname(prevname)
            self.parse_lst(lst)

        # current folder just in case
        self.set_bankname(None)
        self.parse_lst(lst)

        # program folder also just in case
        self.set_bankname(sys.argv[0])
        self.parse_lst(lst)

        # automatically from program folder, only one db3 is allowed
        self.parse_db(db)

        self.set_bankname(None)

        logging.info("names: done")


    def _parse_base(self, filename, callback, reverse_encoding=False):
        encodings = ['utf-8-sig', 'iso-8859-1']
        if reverse_encoding:
            encodings.reverse()
        try:
            testpath = os.path.realpath(filename) #for relative paths
            if testpath in self._loaded_wwnames:
                #logging.debug("names: ignoring already loaded file " + filename)
                return

            #logging.debug("names: testing " + filename)
            if not os.path.isfile(filename):
                return
            logging.info("names: loading " + filename)

            #try encodings until one works
            done = False
            for encoding in encodings:
                try:
                    with open(filename, 'r', encoding=encoding) as infile:
                        callback(infile)
                        done = True
                    break
                except UnicodeDecodeError:
                    #logging.info("names: file %s failed with encoding %s, trying others", filename, encoding)
                    continue

            if not done:
                logging.info("names: error reading file %s (change encoding?)", filename)

        except Exception as e:
            logging.error("names: error reading name file " + filename, e)
        # save even on error to avoid re-reading the same wrong file
        self._loaded_wwnames[testpath] = True


    # Wwise_IDs.h ('header file')
    #
    # C++ namespaces with callable constants, as "NAME = ID". Possible namespaces (all inside from "AK"):
    # - EVENTS > (NAME) = (id)
    # - DIALOGUE_EVENTS > (NAME) = (names)
    # - STATES > (STATE GROUP NAMES) > GROUP = (group name) > STATE > (NAME) = (name)
    # - SWITCHES > (SWITCH GROUP NAMES) > GROUP = (name) > SWITCH > (NAME) = (name)
    # - ARGUMENTS > (ARGUMENT GROUP NAMES) > ARGUMENT = (name) > ARGUMENT_VALUE > (NAME) = (name) #older
    # - GAME_PARAMETERS > (NAME) = (name)
    # - TRIGGERS > (NAME) = (name)
    # - BANKS > (NAME) = (name)
    # - BUSSES > (NAME) = (name)
    # - AUX_BUSSES > (NAME) = (name)
    # - AUDIO_DEVICES > (NAME) = (name)
    # - EXTERNAL_SOURCES : (name)
    # - ENVIRONMENTALS > (ENV NAME) > PROPERTY_SET > (NAME) = (hashname)
    # We can just get (NAME) = (ID) and ignore namespaces, except for GROUP/ARGUMENT/etc "pseudo-namespaces"
    # whose ID actually corresponds to the parent namespace name. Since having to know every "pseudo-namespace"
    # isn't very operative just hash the namespace, and if we find a GROUP = id where the ID already
    # exists (the hashed namespace right before) ignore that id+name.
    def parse_h(self, filename=None):
        if not filename:
            filename = self._make_filepath('Wwise_IDs.h') #maybe should try in ../ too?
        self._parse_base(filename, self._parse_h)

    def _parse_h(self, infile):
        #catch ".. static const AkUniqueID THING = 12345U;" lines
        pattern_ct = re.compile(r"^.+ AkUniqueID ([a-zA-Z_][a-zA-Z0-9_]*) = ([0-9]+).*")
        #catch ".. namespace THING", while ignoring ".. // namespace THING" lines
        pattern_ns = re.compile(r"^.+[^/].+ namespace ([a-zA-Z_][a-zA-Z0-9_]*).*")

        for line in infile:
            match = pattern_ct.match(line)
            if match:
                name, id = match.groups()
                self._add_name(id, name, onrepeat=Names.ONREPEAT_IGNORE)
                continue

            match = pattern_ns.match(line)
            if match:
                id = None
                name, = match.groups()
                self._add_name(id, name)


    # (bankname).txt ('bank content TXT')
    #
    # CSV-like format, with section headers and section data (without spaces)
    # (Section name)\t  ID\t    Name\t  (extra fields and \t depending on section)
    # \t  (id)\t    (name)\t    (...)
    # (xN)
    # (empty line, then next section)
    #
    # Sections may be "Event", "Bus", "In Memory" (wem), "Streamed Audio" (wem), and so on.
    # Ultimately we only need \t(id)\t(name). Extra fields usually include the editor's
    # object path (like "\Events\Default Work Unit\Pause_All" for event "Pause_All", or
    # full giant .wem path like D:\Jenkins\ws\wwise_v2019.2\.....\Bass160 Fight3_2D88AD03.wem)
    # Paths go after 3 tabs (except for wem paths), while other sections use 1 tab.
    # "State" (not "State Groups")'s path is actually the state group (could be separated)
    # Wem names can be anything, so we want to capture any text
    #
    # Encoding on Windows is cp-1252 (has 0xA9=copyright), maybe Linux/Mac would use
    # UTF-8, but those symbols seem only used in comments so shouldn't matter too much
    # (other than Python throwing exceptions on unknown chars). Wwise lets you choose
    # between "ANSI" and "Unicode".
    def parse_txt_bnk(self, filename=None):
        if not filename:
            filename = os.path.splitext(self._bankname)[0] + '.txt'
        self._parse_base(filename, self._parse_txt, reverse_encoding=True)

    def _parse_txt(self, infile):
        #catch: "	1234155799	Play_Thing			\Default Work Unit\Play_Thing	" (with path being optional)
        # must also catch buses like "3D-Submix_Bus"
        #pattern_ph = re.compile("^\t([0-9]+)\t([a-zA-Z_][a-zA-Z0-9_ ]*)(\t\t\t([^\t]+))?.*")
        #pattern_ph = re.compile(r"^\t([0-9]+)\t([^\t]+)(\t\t*?\t*?([^\t]+))?.*")
        bus_starts = ['Bus', 'Audio Bus', 'Auxiliary Bus']
        pattern_ph = re.compile(r"^\t([0-9]+)\t([^\t]+)[\t ]*([^\t]*)[\t ]*([^\t]*)")

        bus_hash = False
        for line in infile:
            if not line:
                continue
            # reset+test bus flag in new sectiona
            if not line.startswith('\t'):
                bus_hash = False
                for bus_start in bus_starts:
                    if line.startswith(bus_start):
                        bus_hash = True #next names will be buses, and may use extended hash
                        break

            match = pattern_ph.match(line)
            if match:
                id, name, info1, info2 = match.groups()
                path, objpath = self._parse_txt_info(info1, info2)

                self._add_name(id, name, objpath=objpath, path=path, exhash=bus_hash)

    # After name there can be comments, paths or objpaths. Not very consistent so do some autodetection
    def _parse_txt_info(self, info1, info2):
        path = ''
        objpath = ''

        if self._is_objpath(info1):
            objpath = info1
        elif self._is_path(info1):
            path = info1

        if self._is_objpath(info2):
            objpath = info2
        elif self._is_path(info2):
            path = info2

        return (path, objpath)

    def _is_objpath(self, info):
        return info and (info.startswith('\\') or info.startswith('//'))

    def _is_path(self, info):
        return info and len(info) > 2 and info[1] == ':' and info[2] == '\\'


    # SoundbanksInfo.xml ('XML metadata')
    # (bankname).xml
    #
    # An XML with info about bank objects. Main targets are:
    # - <(object) Id="(id)" Name="(name)" ...
    # - <(object) Id="(id)" ...>\n ...  <ShortName>(name)</ShortName> <Path>(path)</Path>...
    # Names are hashnames, while ShortNames/Paths may be anything (including UTF-8), usually
    # Paths is the real file ("sfx/file.wav"), while ShortName is may be shared (like multi-lang
    # .wem with same ShortName but different Paths) and can be a hashname. Other attrs include
    # ObjectPath (not too useful, see .txt info). Some tags are just IDs
    # links without name though.
    #
    # The XML can be big (+20MB) and since we don't need all details and just id/names it can be
    # parsed as simple text to increase performance.
    # 
    # Devs may generate one .xml per bnk instead but this is much less common
    #
    # Also some version docs say Wwise generates "SoundbankInfo.xml" (singular) but from SDK samples
    # it's always plural (some games like Far Cry 5 has a SoundbankInfo.xml but format is different).
    def parse_xml(self, filename=None):
        if not filename:
            filename = self._make_filepath('SoundbanksInfo.xml')
        self._parse_base(filename, self._parse_xml)

    def parse_xml_bnk(self, filename=None):
        if not filename:
            filename = os.path.splitext(self._bankname)[0] + '.xml'
        self._parse_base(filename, self._parse_xml)

    def _parse_xml(self, infile):
        #catch: "	<Thing Id="12345" Name="Play_Thing" ObjectPath="\Default Work Unit\Play_Thing">"
        pattern_in = re.compile(r"^.*<.+ Id=[\"]([0-9]+)[\"] .*Name=[\"]([a-zA-Z0-9_]+)[\"](.* ObjectPath=[\"](.+?)[\"])?.+")
        #catch: "	<Thing Id="12345" Language="SFX">"
        #       "		<ShortName>Thing-Stuff.wem</ShortName>"
        pattern_id = re.compile(r"^.*<.+ Id=[\"]([0-9]+)[\"] .+")
        pattern_sn = re.compile(r"^.*<ShortName.*>(.+?)</ShortName.*>")
        pattern_pa = re.compile(r"^.*<Path.*>(.+?)</Path.*>")
        pattern_ob = re.compile(r"^.*<ObjectPath.*>(.+?)</ObjectPath.*>")

        id = name = objpath = path = None
        for line in infile:
            # accept id + name (+ objpath)
            match = pattern_in.match(line)
            if match:
                # prev id + shortname still hanging around
                if id and name:
                    self._add_name(id, name, objpath=objpath, path=path)

                id, name, dummy, objpath = match.groups()
                self._add_name(id, name, objpath=objpath)
                id = name = objpath = path = None
                continue


            # try id (may change multiple times)
            match = pattern_id.match(line)
            if match:
                # prev id + shortname still hanging around
                if id and name:
                    self._add_name(id, name, objpath=objpath, path=path)

                id = name = objpath = path = None
                id, = match.groups()
                continue

            # If id was found (in the above match or a previous one) try parts, id + shortname
            # must exists and the others are optional (possible to get all).
            # This assumes an id is followed by names, could get fooled in some cases.
            if id:
                match = pattern_sn.match(line)
                if match:
                    name, = match.groups()
                    continue

                match = pattern_ob.match(line)
                if match:
                    objpath, = match.groups()
                    continue

                match = pattern_pa.match(line)
                if match:
                    path, = match.groups()
                    continue

        # last id + shortname still hanging around
        if id and name:
            self._add_name(id, name, objpath=objpath, path=path)


    # SoundbanksInfo.json ('JSON metadata')
    # (bankname).json
    #
    # A json equivalent to SoundbanksInfo.xml and (bankname).txt, added in ~2020, format roughly being:
    # "(type)": [
    #    { id: ..., field: ... }
    # ],
    # "(type)": [
    # ....
    #
    # Like other files, to avoid loading the (often massive) .json and handling schema
    # changes, just find an "id" then get all possible fields until next "id".
    def parse_json(self, filename=None):
        if not filename:
            filename = self._make_filepath('SoundbanksInfo.json')
        self._parse_base(filename, self._parse_json)

    def parse_json_bnk(self, filename=None):
        if not filename:
            filename = os.path.splitext(self._bankname)[0] + '.json'
        self._parse_base(filename, self._parse_json)

    def _parse_json(self, infile):
        #catch: '	"Id": "12345" '
        pattern_id = re.compile(r"^[ \t]+[\"]Id[\"]: [\"](.+?)[\"][, \t]*")
        #catch: '	"(field)": "(value)" '
        pattern_fv = re.compile(r"^[ \t]+[\"](.+)[\"]: [\"](.+?)[\"][, \t]*")

        id = name = objpath = path = None
        for line in infile:
            # try id (may change multiple times)
            match = pattern_id.match(line)
            if match:
                # prev id + name still hanging around
                if id and name:
                    self._add_name(id, name, objpath=objpath, path=path)

                id = name = objpath = path = None
                id, = match.groups()
                continue

            # If id was found (in the above match or a previous one) try parts
            # This assumes an id is followed by names, could get fooled in some cases.
            if id:
                match = pattern_fv.match(line)
                if match:
                    field, value = match.groups()
                    if   field == 'Name':
                        name = value
                    elif field == 'ShortName': #treated as name, will be identified as guidname when added
                        name = value
                    elif field == 'ObjectPath':
                        objpath = value
                    elif field == 'Path':
                        path = value
                    continue

        # last id + name still hanging around
        if id and name:
            self._add_name(id, name, objpath=objpath, path=path)


    # wwnames.txt
    #
    # An artificial list of names, with optionally an ID and descriptions, in various forms
    # - "(name) - (id)"
    # - "(name) = (id)"
    # - "(name)\t(id)"
    #
    # Name is always mandatory, and depending on ID:
    # - ID provided: accepts (some) non-valid names, also checks min value for ID (since this list
    #   may be built from software like strings2 it needs to weed out false positives).
    # - ID is 0: accepts (some) non-valid hashnames (Wwise does this for lang IDs like "English(US)"
    #   or busses like "Final Charge Up")
    # - no ID: only valid hashable names are accepted, but lines are split/processed
    def parse_lst(self, filename=None):
        if not filename:
            filename = self._make_filepath('wwnames.txt')
        self._parse_base(filename, self._parse_lst)

    def _parse_lst(self, infile):
        # list of processed names to quickly skips repeats
        processed = {}

        # catch: "name(thing) = id" (ex. "8bit", "English(US)", "3D-Submix_Bus")
        pattern_1 = re.compile(r"^[\t]*([a-zA-Z_0-9][a-zA-Z0-9_()\- ]*)( = )([0-9]+)[ ]*$")

        # catch "name"
        #pattern_2 = re.compile(r"^[\t]*([a-zA-Z_][a-zA-Z0-9_]*)[ ]*$")

        # catch and split non-useful (FNV) characters
        pattern_s1 = re.compile(r'[\t\n\r .<>,;.:{}\[\]()\'"$&/=!\\/#@+\^`´¨?|~]')
        #pattern_s2 = re.compile(r'[?|]')

        for line in infile:
            # ignore comments
            if not line:
                continue
            if line[0] == '#':
                if line.startswith('#@'): # special flags
                    self._cfg.add_config(line)
                continue

            match = pattern_1.match(line)
            if match:
                name, __, id = match.groups()
                if name in processed:
                    continue

                #special meaning of "extended hash" (for objects like buses)
                if id == '0':
                    processed[name] = True
                    self._add_name(None, name, exhash=True, source=NameRow.NAME_SOURCE_EXTRA)
                    continue

            #match = pattern_2.match(line)
            #if match:
            #    name, = match.groups()
            #
            #    if name in processed:
            #        continue
            #    processed[name] = True
            #
            #    self._add_name(None, name, onrepeat=Names.ONREPEAT_BEST, source=NameRow.NAME_SOURCE_EXTRA)
            #    continue

            # get sub-parts of a line and hash those, for scripts that have lines like "C_PlayMusic( bgm_01 )"
            # but we want "bgm_01" as the actual hashname, or XML like "<thing1 thing2='thing3'>"
            elems = pattern_s1.split(line)
            for elem in elems:
                #if pattern_s2.match(elem):
                #    continue
                self._parse_lst_elem(elem, processed)

        return

    def _parse_lst_elem(self, elem, processed):
        # not hashable
        if not elem or elem[0].isdigit() or len(elem) > 100:
            return
        if '|' in elem or '?' in elem:
            return

        # maybe could help
        if '-' in elem:
            elem = elem.replace('-', '_')

        # some elems in .exe have names like "bgm_%d" generated at runtime, simulate by making a bunch of names
        # (ex. MGR "bgm_r%03x_start", KOF13 "game_clear_%d")
        if '%' in elem:
            pos = elem.index('%')
            if pos == 0 or elem.count('%') > 1:
                return

            try:
                fmt = elem[pos+1]
                max = 2
                if fmt == '0':
                    max = int(elem[pos+2])
                    fmt = elem[pos+3]
                    if max > 4:
                        max = 4 #avoid too many names

                if fmt == 'd' or fmt == 'i' or fmt == 'u':
                    base = 10
                elif fmt == 'x' or fmt == 'X':
                    base = 16
                else:
                    return

                rng = range(0, pow(base, max), base)
                for i in rng:
                    elem_fmt = elem % (i)

                    self._parse_lst_elem_add(elem_fmt, processed)
            except (ValueError, IndexError):
                pass #meh
            return

        # some odd game has names ending with _ but shouldn't
        if elem.endswith("_"):
            elem_cut = elem[:-1]
            self._parse_lst_elem_add(elem_cut, processed)

        # it's common to use vars that start with _ but maybe will get a few extra names
        if elem.startswith("_"):
            elem_cut = elem[1:]
            self._parse_lst_elem_add(elem_cut, processed)

        # default
        self._parse_lst_elem_add(elem, processed)
        return

    def _parse_lst_elem_add(self, elem, processed):
        if elem in processed:
            return
        processed[elem] = True

        self._add_name(None, elem, source=NameRow.NAME_SOURCE_EXTRA)


    # wwnames.db3
    #
    # An artificial SQLite DB of names. Not parsed, just prepared to be read on get_name
    #
    # Since a parser may load banks from multiple locations (base, langs, etc) other companion files
    # are read from those paths and added to this class' name list, but this DB is pre-generated and left
    # loaded to be used as-is for all banks so it only makes sense to load once from a single place
    def parse_db(self, filename=None):
        #if filename is None:
        #    filename = 'wwnames.db3' #work dir

        #don't reload DB
        if self._db:
            return
        self._db = SqliteHandler()
        self._db.open(filename)

    def close(self):
        if self._db:
            self._db.close()

    # saves loaded hashnames to .txt
    # (useful to check names when loading generic db/lst of names)
    def save_lst(self, basename=None, path=None, save_all=False, save_companion=False, save_missing=False):
        if not basename:
            basename = 'banks'
        else:
            basename = os.path.basename(basename)
        time = datetime.today().strftime('%Y%m%d%H%M%S')
        outname = 'wwnames-%s-%s.txt' % (basename, time)
        if path:
            outname = os.path.join(path, outname)

        logging.info("names: saving %s" % (outname))

        hashtypes_lines = {}
        default_lines = []

        lines = default_lines
        self._cfg.add_lines(lines)

        names = self._names.values()
        for row in names:
            #save hashnames only, as they can be safely shared between games
            if not row.hashname:
                continue
            #save used names only, unless set to save all
            if not save_all and not row.hashname_used:
                continue
            #save names not in xml/h/etc only, unless set to save extra
            if row.source != NameRow.NAME_SOURCE_EXTRA and not save_companion:
                continue

            if self._cfg.classify:
                hashtypes = row.hashtypes
                if not hashtypes:
                    hashtypes = set()
                    hashtypes.add((wdefs.fnv_no, self.EMPTY_BANKTYPE))

                for hashtype, bank in hashtypes:
                     
                    banks_lines = hashtypes_lines.get(hashtype)
                    if not banks_lines:
                        banks_lines = {}
                        hashtypes_lines[hashtype] = banks_lines

                    sublines = banks_lines.get(bank)
                    if not sublines:
                        sublines = []
                        banks_lines[bank] = sublines

                    self._save_lst_name(row, sublines)
            else:
                self._save_lst_name(row, lines)

        # when this flag is set, lines are saved for each type above and classified 
        # into sections (helps a bit to detect bogus names)
        if self._cfg.classify:
            lines = default_lines #restore

            # may print like: bank > hashtypes (banks_first=True), or hashtypes > banks
            banks_first = True
            if banks_first:
                for bank in sorted(self._loaded_banknames):
                    for hashtype in wdefs.fnv_order:
                        self._include_lines(save_missing, lines, hashtypes_lines, hashtype, bank)
            else:
                for hashtype in wdefs.fnv_order:
                    for bank in sorted(self._loaded_banknames):
                        self._include_lines(save_missing, lines, hashtypes_lines, hashtype, bank)

            lines.append('')

        # write IDs that don't should have hashnames but don't
        if save_missing:
            self._include_missing_all(lines)

        #     for hashtype in wdefs.fnv_order:
        #         if hashtype not in self._missing:
        #             continue
        #         banks = self._missing[hashtype]
        #         for bank in banks:
        #             ids = banks[bank]
        #             if not ids:
        #                 continue

        #             lines.append('')
        #             if bank:
        #                 lines.append('### MISSING %s NAMES (%s)' % (hashtype.upper(), bank))
        #             else:
        #                 lines.append('### MISSING %s NAMES' % (hashtype.upper()))

        #             for id in ids:
        #                 lines.append('# %s' % (id))

        with open(outname, 'w', encoding='utf-8') as outfile:
            outfile.write('\n'.join(lines))

    def _include_lines(self, save_missing, lines, types_lines, hashtype, bank):
        if hashtype not in types_lines:
            return
        banks = types_lines[hashtype]

        if bank not in banks:
            banks_missing = self._missing.get(hashtype)
            if not banks_missing or bank not in banks_missing:
                return

        sublines = banks.get(bank)
        if not sublines and not save_missing:
            return

        lines.append('')
        if bank:
            infobank = self._get_infobank(bank)
            lines.append('### %s NAMES (%s)' % (hashtype.upper(), infobank))
        else:
            lines.append('### %s NAMES' % (hashtype.upper()))

        if sublines:
            sublines.sort(key=str.lower)
            for subline in sublines:
                lines.append(subline)

        # include missing ids at bank level (otherwise at the end)
        if save_missing:
            self._include_missing(lines, hashtype, bank)


    def _include_missing(self, lines, hashtype, bank, header=False):
        if self._cfg.skip_hastype(hashtype):
            return

        banks = self._missing.get(hashtype)
        if not banks:
            return
        ids = banks.get(bank)
        if not ids:
            return

        if header:
            lines.append('')
            if bank:
                infobank = self._get_infobank(bank)
                lines.append('### MISSING %s NAMES (%s)' % (hashtype.upper(), infobank))
            else:
                lines.append('### MISSING %s NAMES' % (hashtype.upper()))

        for id in ids:
            lines.append('# %s' % (id))
        
        # remove so it doesn't get saved twice
        banks[bank] = {}

    def _include_missing_all(self, lines):
        for hashtype in wdefs.fnv_order:
            if hashtype not in self._missing:
                continue
            banks = self._missing[hashtype]
            for bank in banks:
                self._include_missing(lines, hashtype, bank, header=True)

    def _get_infobank(self, bank):
        basebank, _ = os.path.splitext(bank)
        if not basebank.isnumeric():
            return bank

        row = self.get_namerow(basebank)
        if not row or not row.hashname:
            return bank

        return "%s: %s" % (bank, row.hashname)

    def _save_lst_name(self, row, lines):
        #logging.debug("names: using '%s'", row.hashname)
        extended = ''
        if row.extended:
            extended = ' = 0' #allow names with special chars
        lines.append('%s%s' % (row.hashname, extended))

        # log alts too (list should be cleaned up manually)
        for hashname in row.hashnames:
            if extended:
                lines.append('#alt')
                lines.append('%s%s' % (row.hashname, extended))
            else:
                lines.append('%s #alt' % (hashname))


    # saves loaded hashnames to DB
    def save_db(self, save_all=False, save_companion=False):
        logging.info("names: saving db")
        if not self._db or not self._db.is_open():
            #force creation of BD if didn't exist
            #self._db.close() #not needed?
            self._db = SqliteHandler()
            self._db.open(None, preinit=True)

        self._db.save(self._names.values(), save_all=save_all, save_companion=save_companion)


    # banks could come from different paths
    def set_bankname(self, bankname):
        self._bankname = bankname

    # base path + name from a base filename (bank's folder+name)
    def _make_filepath(self, basename, basepath=None):
        if not basepath:
            basepath = self._bankname

        if basepath:
            pathname = os.path.dirname(basepath)
            if pathname:
                filename = os.path.join(pathname, basename)
            else:
                filename = basename
        else:
            filename = basename

        return filename

    def get_weight(self, groupname, valuename):
        return self._cfg.get_weight(groupname, valuename)

    def sort_always(self):
        return self._cfg.sort_always
