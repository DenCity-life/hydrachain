# Copyright (c) 2015 Heiko Hees
import rlp
from base import LockSet, Vote, VoteBlock, VoteNil, Signed
from base import BlockProposal, VotingInstruction, DoubleVotingError, InvalidVoteError
from base import TransientBlock, Block, Proposal, HDCBlockHeader, InvalidProposalError
from utils import cstr, phx
from ethereum.slogging import get_logger
log = get_logger('hdc.consensus')


class ManagerDict(object):

    def __init__(self, dklass, parent):
        self.d = dict()
        self.dklass = dklass
        self.parent = parent

    def __getitem__(self, k):
        if k not in self.d:
            self.d[k] = self.dklass(self.parent, k)
        return self.d[k]

    def __iter__(self):
        return iter(self.d)

    def pop(self, k):
        self.d.pop(k)


class MissingParent(Exception):
    pass


class Synchronizer(object):

    def __init__(self, consensusmanager):
        self.cm = consensusmanager
        self.requested = set()

    def process(self):
        "check which blocks are missing, request and keep track of them"
        self.cm.log('in sync.process', known=len(self.cm.block_candidates),
                    requested=len(self.requested))
        missing = set()
        for p in self.cm.block_candidates.values():
            if not self.cm.get_blockproposal(p.block.prevhash):
                missing.add(p.block.prevhash)
            if p.blockhash in self.requested:
                self.requested.remove(p.blockhash)  # cleanup

        p = self.cm.active_round.proposal
        if isinstance(p, VotingInstruction) and not self.cm.get_blockproposal(p.blockhash):
            missing.add(p.blockhash)
        for blockhash in missing - self.requested:
            self.requested.add(blockhash)
            self.cm.broadcast(BlockRequest(blockhash))
        if self.requested:
            self.cm.log('sync', requested=[phx(bh) for bh in self.requested],
                        missing=len(missing))


class ConsensusContract(object):

    def __init__(self, validators):
        self.validators = validators

    def proposer(self, height, round_):
        v = abs(hash(repr((height, round_))))
        return self.validators[v % len(self.validators)]

    def isvalidator(self, address, height=0):
        assert len(self.validators)
        return address in self.validators

    def isproposer(self, p):
        assert isinstance(p, Proposal)
        return p.sender == self.proposer(p.height, p.round)

    def num_eligible_votes(self, height):
        if height == 0:
            return 0
        return len(self.validators)


class ProtocolFailureEvidence(object):
    evidence = None

    def __repr__(self):
        return '<%s evidence=%r>' % (self.__class__.__name__, self.evidence)


class InvalidProposalEvidence(ProtocolFailureEvidence):

    def __init__(self, proposal):
        self.evidence = proposal


class DoubleVotingEvidence(ProtocolFailureEvidence):

    def __init__(self, vote, othervote):
        self.evidence = (vote, othervote)


class InvalidVoteEvidence(ProtocolFailureEvidence):

    def __init__(self, vote):
        self.evidence = vote


class FailedToProposeEvidence(ProtocolFailureEvidence):

    def __init__(self, round_lockset):
        self.evidence = round_lockset


class ConsensusManager(object):

    def __init__(self, chainservice, consensus_contract, privkey):
        self.chainservice = chainservice
        self.chain = chainservice.chain
        self.contract = consensus_contract
        self.privkey = privkey

        self.synchronizer = Synchronizer(self)
        self.heights = ManagerDict(HeightManager, self)
        self.block_candidates = dict()  # blockhash : BlockProposal

        self.tracked_protocol_failures = list()

        # sign genesis
        if self.head.number == 0:
            v = self.sign(VoteBlock(0, 0, self.head.hash))
            self.add_vote(v)

        # add initial lockset
        head_proposal = self.load_proposal(self.head.hash)
        if head_proposal:
            for v in head_proposal.signing_lockset:
                self.add_vote(v)

        assert self.contract.isvalidator(self.coinbase)

    # pesist proposals

    def store_proposal(self, p):
        assert isinstance(p, BlockProposal)
        self.chainservice.db.put('blockproposal:%s' % p.blockhash, BlockProposal.serialize(p))

    def load_proposal_rlp(self, blockhash):
        try:
            return self.chainservice.db.get('blockproposal:%s' % blockhash)
        except KeyError:
            return None

    def load_proposal(self, blockhash):
        prlp = self.load_proposal_rlp(blockhash)
        if prlp:
            return BlockProposal.deserialize(prlp)

    def get_blockproposal(self, blockhash):
        return self.block_candidates.get(blockhash) or self.load_proposal(blockhash)

    def has_blockproposal(self, blockhash):
        return bool(self.load_proposal_rlp(blockhash))

    def blockproposal_by_height(self, height):
        assert 0 < height < self.height
        bh = self.chainservice.chain.index.get_block_by_number(height)
        return self.load_proposal(bh)

    @property
    def coinbase(self):
        return self.chain.coinbase

    def __repr__(self):
        return '<CP A:%r H:%d R:%d L:%r %s>' % (phx(self.coinbase), self.height, self.round,
                                                self.last_lock, self.active_round.lockset.state)

    def log(self, tag, **kargs):
        # if self.coinbase != 0: return
        t = int(self.chainservice.now)
        c = lambda x: cstr(self.coinbase, x)
        msg = ' '.join([str(t), c(repr(self)),  tag, (' %r' % kargs if kargs else '')])
        log.debug(msg)

    @property
    def head(self):
        return self.chain.head

    @property
    def height(self):
        return self.head.number + 1

    @property
    def round(self):
        return self.heights[self.height].round

    # message handling

    def broadcast(self, m):
        self.log('broadcasting', msg=m)
        self.chainservice.broadcast(m)

    def add_vote(self, v):
        assert isinstance(v, Vote)
        assert self.contract.isvalidator(v.sender)
        # exception for externaly received votes signed by self, necessary for resyncing
        is_own_vote = bool(v.sender == self.coinbase)
        try:
            success = self.heights[v.height].add_vote(v, force_replace=is_own_vote)
        except DoubleVotingError:
            ls = self.heights[v.height].rounds[v.round].lockset
            self.tracked_protocol_failures.append(DoubleVotingEvidence(v, ls))
            log.warn('double voting detected', vote=v, ls=ls)
        return success

    def add_proposal(self, p):
        def check(valid):
            if not valid:
                self.tracked_protocol_failures.append(InvalidProposalEvidence(p))
                log.warn('invalid proposal', p=p)
                raise InvalidProposalError()
            return True

        assert isinstance(p, Proposal)
        self.log('cm.add_proposal', p=p)
        if p.height < self.height:
            self.log('proposal from the past')
            return

        if not check(self.contract.isvalidator(p.sender) and self.contract.isproposer(p)):
            return
        if not check(p.lockset.is_valid):
            return
        if not check(p.lockset.height == p.height or p.round == 0):
            return
        if not check(p.round - p.lockset.round == 1 or p.round == 0):
            return
        for v in p.lockset:
            self.add_vote(v)  # implicitly checks their validity
        if isinstance(p, BlockProposal):
            if not check(p.block.number == p.height):
                return
            if not check(p.lockset.has_noquorum or p.round == 0):
                return
            # validation
            if p.height > self.height:
                self.log('proposal from the future, not in sync FIXME')
                return
            blk = self.chainservice.link_block(p.block)
            if not check(blk):
                return
            p.block = blk  # block linked to chain
            self.add_block_proposal(p)  # implicitly checks the votes validity
        else:
            assert isinstance(p, VotingInstruction)
            if not check(p.lockset.has_quorum_possible):
                return
        is_valid = self.heights[p.height].add_proposal(p)
        return is_valid  # can be broadcasted

    def add_block_proposal(self, p):
        assert isinstance(p, BlockProposal)
        if self.has_blockproposal(p.blockhash):
            self.log('known block_proposal')
            return
        assert p.signing_lockset.has_quorum  # on previous block
        assert p.signing_lockset.height == p.height - 1
        for v in p.signing_lockset:
            self.add_vote(v)
        self.block_candidates[p.blockhash] = p

    @property
    def last_committing_lockset(self):
        return self.heights[self.height - 1].last_quorum_lockset

    @property
    def last_valid_lockset(self):
        return self.heights[self.height].last_valid_lockset or self.last_committing_lockset

    @property
    def last_lock(self):
        return self.heights[self.height].last_lock

    @property
    def last_blockproposal(self):
        # valid block proposal on currrent height
        p = self.heights[self.height].last_voted_blockproposal
        if p:
            return p
        elif self.height > 1:  # or last block
            return self.get_blockproposal(self.head.hash)

    @property
    def active_round(self):
        hm = self.heights[self.height]
        return hm.rounds[hm.round]

    def setup_alarm(self):
        ar = self.active_round
        delay = ar.setup_alarm()
        if delay is not None:
            self.chainservice.setup_alarm(delay, self.on_alarm, ar)
            self.log('set up alarm', now=self.chainservice.now,
                     delay=delay, triggered=delay + self.chainservice.now)

    def on_alarm(self, ar):
        # self.log('on alarm')
        assert isinstance(ar, RoundManager)
        if self.active_round == ar:
            self.log('on alarm, matched', ts=self.chainservice.now)
            self.process()

    def process(self):
        self.log('in process')
        self.commit()
        self.heights[self.height].process()
        self.commit()
        self.cleanup()
        # self.synchronizer.process()
        self.setup_alarm()

        for f in self.tracked_protocol_failures:
            if not isinstance(f, FailedToProposeEvidence):
                log.warn('protocol failure', incident=f)

    start = process

    def commit(self):
        self.log('in commit')
        for p in [c for c in self.block_candidates.values() if c.block.prevhash == self.head.hash]:
            assert isinstance(p, BlockProposal)
            if self.heights[p.height].has_quorum == p.blockhash:
                self.store_proposal(p)
                success = self.chainservice.commit_block(p.block)
                assert success
                if success:
                    self.log('commited', p=p, hash=phx(p.blockhash))
                    assert self.head == p.block
                    self.commit()
                    return

    def cleanup(self):
        self.log('in cleanup')
        for p in self.block_candidates.values():
            if self.head.number >= p.height:
                self.block_candidates.pop(p.blockhash)
        for h in list(self.heights):
            if self.heights[h].height < self.head.number:
                self.heights.pop(h)

    def mk_lockset(self, height):
        return LockSet(num_eligible_votes=self.contract.num_eligible_votes(height))

    def sign(self, o):
        assert isinstance(o, Signed)
        return o.sign(self.privkey)


class HeightManager(object):

    def __init__(self, consensusmanager, height=0):
        self.cm = consensusmanager
        self.log = self.cm.log
        self.height = height
        self.rounds = ManagerDict(RoundManager, self)
        log.debug('A:%s Created HeightManager H:%d' % (phx(self.cm.coinbase), self.height))

    @property
    def round(self):
        l = self.last_valid_lockset
        if l:
            return l.round + 1
        return 0

    @property
    def last_lock(self):
        "highest lock on height"
        for r in reversed(sorted(self.rounds)):
            if self.rounds[r].lock is not None:
                return self.rounds[r].lock

    @property
    def last_voted_blockproposal(self):
        "the last block proposal node voted on"
        for r in reversed(sorted(self.rounds)):
            if isinstance(self.rounds[r].proposal, BlockProposal):
                assert isinstance(self.rounds[r].lock, Vote)
                if self.rounds[r].proposal.blockhash == self.rounds[r].lock.blockhash:
                    return self.rounds[r].proposal

    @property
    def last_valid_lockset(self):
        "highest valid lockset on height"
        for r in reversed(sorted(self.rounds)):
            ls = self.rounds[r].lockset
            if ls.is_valid:
                return ls
        return None

    @property
    def last_quorum_lockset(self):
        found = None
        for r in sorted(self.rounds):
            ls = self.rounds[r].lockset
            if ls.is_valid and ls.has_quorum:
                assert found is None  # consistency check, only one quorum allowed
                found = ls
        return found

    @property
    def has_quorum(self):
        ls = self.last_quorum_lockset
        if ls:
            return ls.has_quorum

    def add_vote(self, v, force_replace=False):
        return self.rounds[v.round].add_vote(v, force_replace)

    def add_proposal(self, p):
        assert p.height == self.height
        assert p.lockset.is_valid
        if p.round > self.round:
            self.round = p.round
        return self.rounds[p.round].add_proposal(p)

    def process(self):
        self.log('in hm.process', height=self.height)
        self.rounds[self.round].process()


class RoundManager(object):

    timeout = 1  # secs
    timeout_round_factor = 1.2

    def __init__(self, heightmanager, round_=0):
        assert isinstance(round_, int)
        self.round = round_

        self.hm = heightmanager
        self.cm = heightmanager.cm
        self.log = self.hm.log
        self.height = heightmanager.height
        self.lockset = self.cm.mk_lockset(self.height)
        self.proposal = None
        self.lock = None
        self.timeout_time = None
        log.debug('A:%s Created RoundManager H:%d R:%d' %
                  (phx(self.cm.coinbase), self.hm.height, self.round))

    def setup_alarm(self):
        "setup a timeout for waiting for a proposal"
        if self.timeout_time is not None or self.proposal:
            return
        now = self.cm.chainservice.now
        delay = self.timeout * self.timeout_round_factor ** self.round
        self.timeout_time = now + delay
        return delay

    def add_vote(self, v, force_replace=False):
        self.log('rm.adding', vote=v, proposal=self.proposal, pid=id(self.proposal))
        try:
            success = self.lockset.add(v, force_replace)
        except InvalidVoteError:
            self.cm.tracked_protocol_failures.append(InvalidVoteEvidence(v))
            return
        # report failed proposer
        if self.lockset.is_valid:
            self.log('lockset is valid', ls=self.lockset)
            if not self.proposal and self.lockset.has_noquorum:
                self.cm.tracked_protocol_failures.append(FailedToProposeEvidence(self.lockset))
        return success

    def add_proposal(self, p):
        self.log('rm.adding', proposal=p, old=self.proposal)
        assert isinstance(p, Proposal)
        assert isinstance(p, VotingInstruction) or isinstance(p.block, Block)  # already linked
        assert not self.proposal or self.proposal == p
        self.proposal = p
        return True

    def process(self):
        self.log('in rm.process', height=self.hm.height, round=self.round)

        assert self.cm.round == self.round
        assert self.cm.height == self.hm.height == self.height
        p = self.propose()
        if isinstance(p, BlockProposal):
            self.cm.add_block_proposal(p)
        if p:
            self.cm.broadcast(p)
        v = self.vote()
        if v:
            self.cm.broadcast(v)
        assert not self.proposal or self.lock

    def mk_proposal(self, round_lockset=None):
        signing_lockset = self.cm.last_committing_lockset.copy()  # quorum which signs prev block
        if self.round > 0:
            round_lockset = self.cm.last_valid_lockset.copy()
            assert round_lockset.has_noquorum
        else:
            round_lockset = None
        assert signing_lockset.has_quorum
        # for R0 (std case) we only need one lockset!
        assert round_lockset is None or self.round > 0
        block = self.cm.chain.head_candidate
        # fix pow
        block.header.__class__ = HDCBlockHeader
        bp = BlockProposal(self.height, self.round, block, signing_lockset, round_lockset)
        self.cm.sign(bp)
        return bp

    def propose(self):
        proposer = self.cm.contract.proposer(self.height, self.round)
        self.log('in propose', proposer=phx(proposer), proposal=self.proposal, lock=self.lock)
        if proposer != self.cm.coinbase:
            return
        if self.proposal:
            assert self.proposal.sender == self.cm.coinbase
            assert self.lock
            return

        round_lockset = self.cm.last_valid_lockset
        self.log('in creating proposal', round_lockset=round_lockset)
        if self.round == 0 or round_lockset.has_noquorum:
            proposal = self.mk_proposal()
        elif round_lockset.has_quorum_possible:
            proposal = VotingInstruction(self.height, self.round, round_lockset.copy())
            self.cm.sign(proposal)
        else:
            raise Exception('invalid round_lockset')

        self.log('created proposal', p=proposal)
        self.proposal = proposal
        return proposal

    def vote(self):
        if self.lock:
            return  # voted in this round
        self.log('in vote', proposal=self.proposal, pid=id(self.proposal))

        # get last lock on height
        last_lock = self.hm.last_lock

        if self.proposal:
            if isinstance(self.proposal, VotingInstruction):
                assert self.proposal.lockset.has_quorum_possible
                self.log('voting on instruction')
                v = VoteBlock(self.height, self.round, self.proposal.blockhash)
            elif not isinstance(last_lock, VoteBlock):
                assert isinstance(self.proposal, BlockProposal)
                assert isinstance(self.proposal.block, Block)  # already linked to chain
                assert self.proposal.lockset.has_noquorum or self.round == 0
                assert self.proposal.block.prevhash == self.cm.head.hash
                self.log('voting proposed block')
                v = VoteBlock(self.height, self.round, self.proposal.blockhash)
            else:  # repeat vote
                self.log('voting on last vote')
                v = VoteBlock(self.height, self.round, last_lock.blockhash)
        elif self.timeout_time is not None and self.cm.chainservice.now >= self.timeout_time:
            if isinstance(last_lock, VoteBlock):  # repeat vote
                self.log('timeout voting on last vote')
                v = VoteBlock(self.height, self.round, last_lock.blockhash)
            else:
                self.log('timeout voting not locked')
                v = VoteNil(self.height, self.round)
        else:
            return
        self.cm.sign(v)

        self.log('voted', vote=v)
        self.lock = v
        assert self.hm.last_lock == self.lock
        self.lockset.add(v)
        return v
