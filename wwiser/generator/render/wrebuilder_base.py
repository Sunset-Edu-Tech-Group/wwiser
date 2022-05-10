import logging
from . import wnode_misc, wnode_source, wnode_rtpc, wnode_transitions, wnode_tree
from ..txtp import wtxtp_info


#beware circular refs
#class CAkNode(object):
#    def __init__(self):
#       pass #no params since changing constructors is a pain

# common for all 'rebuilt' nodes
class CAkHircNode(object):
    def __init__(self):
        pass #no params since changing constructors is a pain

    def init_builder(self, builder):
        self.builder = builder

    def init_node(self, node):
        #self.version = node.get_root().get_version()
        self.node = node
        self.name = node.get_name()
        self.nsid = node.find1(type='sid')
        self.sid = None
        if self.nsid:
            self.sid = self.nsid.value()

        self.config = wnode_misc.NodeConfig()
        self.fields = wtxtp_info.TxtpFields() #main node fields, for printing
        self.stingers = []

        self._build(node)

    #--------------------------------------------------------------------------

    def _barf(self, text="not implemented"):
        raise ValueError("%s - %s %s" % (text, self.name, self.sid))

    def _process_next(self, ntid, txtp, nbankid=None):
        tid = ntid.value()
        if tid == 0:
            #this is fairly common in switches, that may define all combos but some nodes don't point to anything
            return

        if nbankid:
            # play actions reference bank by id (plus may save bankname in STID)
            bank_id = nbankid.value()
        else:
            # try same bank as node
            bank_id = ntid.get_root().get_id()

        bnode = self.builder._get_bnode_by_ref(bank_id, tid, sid_info=self.sid, nbankid_info=nbankid)
        if not bnode:
            return

        # filter HIRC nodes (for example drop unwanted calls to layered ActionPlay)
        if self.builder._filter and self.builder._filter.active:
            generate = self.builder._filter.allow_inner(bnode.node, bnode.nsid)
            if not generate:
                return

        #logging.debug("next: %s %s > %s", self.node.get_name(), self.sid, tid)
        bnode._make_txtp(txtp)
        return

    #--------------------------------------------------------------------------

    # info when generating transitions
    def _register_transitions(self, txtp):
        for ntid in self.ntransitions:
            node = self.builder._get_transition_node(ntid)
            txtp.transitions.add(node)
        return

    #--------------------------------------------------------------------------

    def _build(self, node):
        self._barf()
        return


    WARN_PROPS = [
        #"[TrimInTime]", "[TrimOutTime]", #seen in CAkState (ex. DMC5)
        #"[FadeInCurve]", "[FadeOutCurve]", #seen in CAkState, used in StateChunks (ex. NSR)
        "[LoopStart]", "[LoopEnd]",
        "[FadeInTime]", "[FadeOutTime]", "[LoopCrossfadeDuration]",
        "[CrossfadeUpCurve]", "[CrossfadeDownCurve]",
        #"[MakeUpGain]", #seems to be used when "auto normalize" is on (ex. Magatsu Wahrheit, MK Home Circuit)
        #"[BusVolume]", #percent of max? (ex. DmC)
        #"[OutputBusVolume]"
    ]
    OLD_AUDIO_PROPS = [
        'Volume', 'Volume.min', 'Volume.max', 'LFE', 'LFE.min', 'LFE.max',
        'Pitch', 'Pitch.min', 'Pitch.max', 'LPF', 'LPF.min', 'LPF.max',
    ]
    OLD_ACTION_PROPS = [
        'tDelay', 'tDelayMin', 'tDelayMax', 'TTime', 'TTimeMin', 'TTimeMax',
    ]

    def __parse_props(self, ninit):
        nvalues = ninit.find(name='AkPropBundle<AkPropValue,unsigned char>')
        if not nvalues:
            nvalues = ninit.find(name='AkPropBundle<float,unsigned short>')
        if not nvalues:
            nvalues = ninit.find(name='AkPropBundle<float>')
        if nvalues: #newer
            nprops = nvalues.finds(name='AkPropBundle')
            for nprop in nprops:
                nkey = nprop.find(name='pID')
                nval = nprop.find(name='pValue')

                valuefmt = nkey.get_attr('valuefmt')
                value = nval.value()
                if any(prop in valuefmt for prop in self.WARN_PROPS):
                    #self._barf('found prop %s' % (valuefmt))
                    self.builder._unknown_props[valuefmt] = True

                elif "[Loop]" in valuefmt:
                    self.config.loop = value

                elif "[Volume]" in valuefmt:
                    self.config.volume = value

                elif "[MakeUpGain]" in valuefmt:
                    self.config.makeupgain = value

                elif "[Pitch]" in valuefmt:
                    self.config.pitch = value

                elif "[DelayTime]" in valuefmt:
                    self.config.delay = value

                elif "[InitialDelay]" in valuefmt:
                    self.config.idelay = value * 1000.0 #float in seconds to ms

                #missing useful effects:
                #TransitionTime: used in play events to fade-in event

                self.fields.keyval(nkey, nval)

        #todo ranged values
        nranges = ninit.find(name='AkPropBundle<RANGED_MODIFIERS<AkPropValue>>')
        if nranges: #newer
            nprops = nranges.finds(name='AkPropBundle')
            for nprop in nprops:
                nkey = nprop.find(name='pID')
                nmin = nprop.find(name='min')
                nmax = nprop.find(name='max')

                self.fields.keyminmax(nkey, nmin, nmax)

        return nvalues or nranges


    def _build_action_config(self, node):
        ninit = node.find1(name='ActionInitialValues') #used in action objects (CAkActionX)
        if not ninit:
            return

        ok = self.__parse_props(ninit)
        if ok:
            return

        #todo
        #may use PlayActionParams + eFadeCurve when TransitionTime is used to make a fade-in (goes after delay)

        #older
        for prop in self.OLD_ACTION_PROPS:
            nprop = ninit.find(name=prop)
            if not nprop:
                continue
            value = nprop.value()

            #fade-in curve
            #if value != 0 and (prop == 'TTime' or prop == 'TTimeMin'):
            #    self._barf("found " + prop)

            if value != 0 and (prop == 'tDelay' or prop == 'tDelayMin'):
                self.config.idelay = value

            if value != 0: #default to 0 if not set
                self.fields.prop(nprop)


    def _build_audio_config(self, node):
        name = node.get_name()

        # find songs that silence files to crossfade
        # mainly useful on Segment/Track level b/c usually games that set silence on
        # Switch/RanSeq do nothing interesting with it (ex. just to silence the whole song)
        check_state = name in ['CAkMusicTrack', 'CAkMusicSegment']
        check_rtpc = check_state
        nbase = node.find1(name='NodeBaseParams')
        if nbase and check_state:
            # state sets volume states to silence tracks (ex. MGR)
            # in rare cases those states are also used to slightly increase volume (Monster Hunter World's 3221323256.bnk)
            nstatechunk = nbase.find1(name='StateChunk')
            if nstatechunk:
                nstategroups = nstatechunk.finds(name='AkStateGroupChunk') #probably only one but...
                for nstategroup in nstategroups:
                    nstates = nstategroup.finds(name='AkState')
                    if not nstates: #possible to have groupchunks without pStates (ex Xcom2's 820279197)
                        continue

                    bank_id = nstategroup.get_root().get_id()
                    for nstate in nstates:
                        nstateinstanceid = nstate.find1(name='ulStateInstanceID')
                        if not nstateinstanceid: #???
                            continue
                        tid = nstateinstanceid.value()

                        # state should exist as a node and have a volume value (states for other stuff are common)
                        bstate = self.builder._get_bnode_by_ref(bank_id, tid, self.sid)
                        has_volumes = bstate and bstate.config.volume
                        if not has_volumes:
                            continue

                        self.config.crossfaded = True

                        logging.debug("generator: state volume found %s %s %s" % (self.sid, tid, node.get_name()))
                        nstategroupid = nstategroup.find1(name='ulStateGroupID') #parent group

                        nstateid = nstate.find1(name='ulStateID')
                        if nstategroupid and nstateid:
                            self.config.add_volume_state(nstategroupid, nstateid, bstate.config)
                            self.fields.keyvalvol(nstategroupid, nstateid, bstate.config.volume)

        if nbase and check_rtpc:
            # RTPC linked to volume (ex. DMC5 battle rank layers, ACB whispers)
            self._build_rtpc_config(nbase)

        # find other parameters
        ninit = node.find1(name='NodeInitialParams') #most objects that aren't actions nor states
        if not ninit:
            ninit = node.find1(name='StateInitialValues') #used in CAkState
        if not ninit:
            return

        ok = self.__parse_props(ninit)
        if ok:
            return

        #older
        for prop in self.OLD_AUDIO_PROPS:
            nprop = ninit.find(name=prop)
            if not nprop:
                continue
            value = nprop.value()
            if value != 0 and prop == 'Volume':
                self.config.volume = value #also min/max

            if value != 0: #default to 0 if not set
                self.fields.prop(nprop)

    def _build_rtpc_config(self, node):
        rtpcs = wnode_rtpc.AkRtpcList(node)
        if rtpcs.has_volume_rtpcs:
            self.config.rtpcs = rtpcs
            self.config.crossfaded = True
            for nid, minmax in rtpcs.fields:
                self.fields.rtpc(nid, minmax)
        return

    def _build_transition_rules(self, node, is_switch):
        rules = wnode_transitions.AkTransitionRules(node)
        for ntid in rules.ntrn:
            if ntid.value() == 0:
                continue
            if is_switch:
                self.ntransitions.append(ntid)
            else:
                # rare in playlists (Polyball, Spiderman)
                self.builder.report_transition_object()
        return

    def _build_tree(self, node):
        return wnode_tree.AkDecisionTree(node)

    def _build_stingers(self, node):
        nstingers = node.finds(name='CAkStinger')
        if not nstingers:
            return

        for nstinger in nstingers:
            stinger = wnode_misc.CAkStinger(nstinger)
            if stinger.tid:
                self.stingers.append(stinger)
        return

    def _build_silence(self, node, clip):
        sound = wnode_misc.NodeSound()
        sound.nsrc = node
        sound.silent = True
        sound.clip = clip
        return sound

    def _parse_source(self, nbnksrc):
        source = wnode_source.AkBankSource(nbnksrc, self.sid)

        if source.is_plugin_silence:
            nsize = nbnksrc.find(name='uSize')
            if nsize and nsize.value():
                # older games have inline plugin info
                source.plugin_fx = self._parse_sfx(nbnksrc, source.plugin_id)
            else:
                # newer games use another CAkFxCustom (though in theory could inline)
                bank_id = source.nsrc.get_root().get_id()
                tid = source.tid
                bfxcustom = self.builder._get_bnode_by_ref(bank_id, tid, self.sid)
                if bfxcustom:
                    source.plugin_fx = bfxcustom.fx

        return source

    def _parse_sfx(self, node, plugin_id):
        fx = wnode_misc.NodeFx(node, plugin_id)
        return fx

    #--------------------------------------------------------------------------

    def _make_txtp(self, txtp):
        try:
            txtp.info.next(self.node, self.fields, nsid=self.nsid)
            self._process_txtp(txtp)
            txtp.info.done()
        except Exception: #as e #autochained
            raise ValueError("Error processing TXTP for node %i" % (self.sid)) #from e

    def _process_txtp(self, txtp):
        self._barf("must implement")