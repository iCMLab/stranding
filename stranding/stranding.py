import logging
import warnings
from collections import namedtuple

from Bio.pairwise2 import align, format_alignment
from Bio.Seq import Seq

from seqseek import Chromosome

from .exceptions import (MissingReferenceFlank,
                        InconsistentAlignment,
                        Unstrandable,
                        FlanksTooShort)


LOGGER = logging.getLogger(__name__)
DEFAULT_MIN_FLANK_LENGTH = 15
DEFAULT_WINDOW_EXTENSION = 0


FORWARD_STRAND = 1
REVERSE_STRAND = -1


# empirically derived default values from stranding hundreds of thousands of flanks
# from an Illumina beadchip. Two points are awarded for each matching base and one
# point is subtracted for each mismatch. Gaps are strongly discouraged with a 5 point
# penalty.
DEFAULT_MATCH_SCORE = 2
DEFAULT_MISMATCH_PENALTY = -1
DEFAULT_GAP_OPEN_PENALTY = -5
DEFAULT_TOLERANCE = 0.77


class Outcome(object):
    def __init__(self, reference, query, strand):
        self.reference = reference
        self.query = query
        self.strand = strand


class GenomeStranding(object):

    def __init__(self,
                 min_flank_length=DEFAULT_MIN_FLANK_LENGTH,
                 tolerance=DEFAULT_TOLERANCE,
                 match_score=DEFAULT_MATCH_SCORE,
                 mismatch_penalty=DEFAULT_MISMATCH_PENALTY,
                 gap_open_penalty=DEFAULT_GAP_OPEN_PENALTY):

            self.min_flank_length = min_flank_length
            self.tolerance = tolerance
            self.match_score = match_score
            self.mismatch_penalty = mismatch_penalty
            self.gap_open_penalty = gap_open_penalty
            if self.min_flank_length < DEFAULT_MIN_FLANK_LENGTH:
                warnings.warn('Short flank lengths may lead to inaccurate alignments')

    def is_high_scoring(self, score, query):
        if len(query) < self.min_flank_length:
            return False
        return score > len(query) * self.match_score * self.tolerance

    def is_perfect_score(self, score, query):
        if len(query) < self.min_flank_length:
            return False
        return score == len(query) * self.match_score

    def align(self, ref, query, score_only=True):
        alignment = align.localms(ref, query, self.match_score, self.mismatch_penalty,
                                  self.gap_open_penalty, self.mismatch_penalty,
                                  score_only=score_only)
        if score_only and not alignment:
            return 0
        return alignment

    def align_and_log(self, ref, query):
        alignments = self.align(ref, query, False)
        for a in alignments:
            if self.is_high_scoring(a, query):
                LOGGER.error(format_alignment(*a))

    def strand_flanks(self, _5p, _3p, build, chr_name, pos, window=DEFAULT_WINDOW_EXTENSION):
        """
        This is a flank stranding algorithm for sequences mapped to a human genome
        reference assembly. Mapping coordinates are required! This is not BLAT or BLAST.

        Given one or both flanks and genome mapping coordinates it determines
        if the flanking sequence(s) coorespond to the forward or reverse strand of the
        specified reference assembly.

        It can optionally look beyond exact mapping coordinates to search nearby regions
        (up to the `window` size specified) but takes longer as local alignments are
        expensive on long sequences.

        The `tolerance` setting defines the minimum alignment score relative to the
        query sequence length. This is also impacted by changes to the alignment
        scoring parameters

        When `tolerance` is 1.0 and `window` is 0.0 the algorithm will only check for
        exact sequence matches at the specified coordinates. This is the most performant
        use case as no alignments are performed.

        Otherwise, the algorithm will load the reference sequences for the 5' and 3'
        flanks at the specified coordinates extending in each direction extended by
        `window`. These sequences and their reverse complements are aligned and
        scored against the query flanks. Alignments scoring above
        `len(query flank) * match_score * tolerance` are accepted.
        (a perfect alignment has a score of `len(query flank) * match_score`)

        A return value of 1 indicates that alignments were accepted against the forward
        reference sequence and the flanks are on the forward strand of the specified
        reference assembly.

        A return value of -1 indicates that alignments were accepted against the
        reverse complement of the forward reference sequence and the flanks correspond
        to the "reverse" or "minus" strand of the specified reference assembly.

        An InconsistentAlignment exception is raised if alignments are accepted on
        both strands. An Unstrandable exception is raised if no alignments are accepted.
        """
        # sanity checks
        if pos == 0:
            raise Unstrandable('Position 0 is unmapped')
        elif chr_name in ('0', 0):
            raise Unstrandable('Chromosome 0 is unmapped')
        elif max(len(_5p), len(_3p)) < self.min_flank_length:
            raise FlanksTooShort('At least one flank must be longer than the specified'
                                 'minimum flank length of s %d' % self.min_flank_length)

        # chromosome-specific conventions
        loop = chr_name == 'MT'
        chr_name = chr_name if chr_name != 'XY' else 'X'
        max_length = max(len(_5p), len(_3p))

        # reference sequences
        try:
            chromosome = Chromosome(chr_name, build, loop=loop)
            ref_5p = chromosome.sequence(pos - window - max_length, pos + window)
            ref_3p = chromosome.sequence(pos - window, pos + max_length + window + 1)
        except ValueError:
            raise MissingReferenceFlank(
                'Could not find flanks for %s %d %d' % (chr_name, pos, window))

	# exact comparisons are cheap so try this first
        if window == 0 and (_3p == ref_3p or _5p == ref_5p):
            return FORWARD_STRAND

        ref_5p_RC = str(Seq(ref_5p).reverse_complement())
        ref_3p_RC = str(Seq(ref_3p).reverse_complement())

        if window == 0 and (_3p == ref_5p_RC or _5p == ref_3p_RC):
            return REVERSE_STRAND

        if window == 0 and self.tolerance == 1.0:
            raise Unstrandable('Strict stranding failed')

        outcomes = [
                Outcome(ref_5p, _5p, FORWARD_STRAND),
                Outcome(ref_3p, _3p, FORWARD_STRAND),
                Outcome(ref_5p_RC, _3p, REVERSE_STRAND),
                Outcome(ref_3p_RC, _5p, REVERSE_STRAND)
            ]

        for outcome in outcomes:
            outcome.score = self.align(outcome.reference, outcome.query)
            if self.is_perfect_score(outcome.score, outcome.query):
                return outcome.strand

        is_fwd = any(self.is_high_scoring(o.score, o.query) for o in outcomes if o.strand == FORWARD_STRAND)
        is_rev = any(self.is_high_scoring(o.score, o.query) for o in outcomes if o.strand == REVERSE_STRAND)

        if is_fwd and is_rev:
            # Alignments were accepted on both strands (!)
            # The flanks may be too short or the tolerance may be too loose.
            LOGGER.error('Forward alignments')
            self.align_and_log(ref_5p, _5p)
            self.align_and_log(ref_3p, _3p)
            LOGGER.error('Reverse alignments')
            self.align_and_log(ref_5p_RC, _3p)
            self.align_and_log(ref_3p_RC, _5p)
            raise InconsistentAlignment('Inconsistent alignments')
        elif is_fwd:
            return FORWARD_STRAND
        elif is_rev:
            return REVERSE_STRAND
        raise Unstrandable('No matching alignments')
