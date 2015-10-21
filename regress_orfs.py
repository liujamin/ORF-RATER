#! /usr/bin/env python

import argparse
import os
import pysam
import pandas as pd
import numpy as np
from scipy.optimize import nnls
import scipy.sparse
import multiprocessing as mp
from hashed_read_genome_array import HashedReadBAMGenomeArray, ReadKeyMapFactory, read_length_nmis, get_hashed_counts
from plastid.genomics.roitools import SegmentChain, positionlist_to_segments
import sys
from time import strftime

parser = argparse.ArgumentParser(description='Use linear regression to identify likely sites of translation. Regression will be performed for ORFs '
                                             'defined by find_orfs_and_types.py using a metagene profile constructed from annotated CDSs. If '
                                             'multiple ribosome profiling datasets are to be analyzed separately (e.g. if they were collected under '
                                             'different drug treatments), then this program should be run separately for each, ideally in separate '
                                             'subfolders indicated by SUBDIR.')

parser.add_argument('bamfiles', nargs='+', help='Path to transcriptome-aligned BAM file(s) for read data')
parser.add_argument('--subdir', default=os.path.curdir,
                    help='Convenience argument when dealing with multiple datasets. In such a case, set SUBDIR to an appropriate name (e.g. HARR, '
                         'CHX) to avoid file conflicts. (Default: current directory)')
parser.add_argument('--restrictbystarts', nargs='+',
                    help='Subdirectory/subdirectories or filename(s) containing regression output to use to restrict ORFs for regression. If a '
                         'directory or list of directories, file(s) of name REGRESSFILE (regression.h5 by default) will be searched for within them. '
                         'For use to restrict regression on e.g. CHX or no-drug data based only on positive hits from e.g. HARR or LTM data. '
                         'Value(s) of MINWSTART indicate the minimum W statistic to require. If multiple directories/files are provided, start '
                         'sites will be taken from their union.')
parser.add_argument('--minwstart', type=float, nargs='+', default=[0],
                    help='Minimum W_start statistic to require for regression output in RESTRICTBYSTARTS. If only one value is given, it will be '
                         'assumed to apply to all; if multiple values are given, the number of values must match the number of values provided for '
                         'RESTRICTBYSTARTS. Ignored if RESTRICTBYSTARTS not included. (Default: 0)')
parser.add_argument('--orfstore', default='orf.h5',
                    help='Path to pandas HDF store containing ORFs to regress; generated by find_orfs_and_types.py (Default: orf.h5)')
parser.add_argument('--inbed', default='transcripts.bed', help='Transcriptome BED-file (Default: transcripts.bed)')
parser.add_argument('--offsetfile', default='offsets.txt',
                    help='Path to 2-column tab-delimited file with 5\' offsets for variable P-site mappings. First column indicates read length, '
                         'second column indicates offset to apply. Read lengths are calculated after trimming up to MAX5MIS 5\' mismatches. Accepted '
                         'read lengths are defined by those present in the first column of this file. If SUBDIR is set, this file is assumed to be '
                         'in that directory. (Default: offsets.txt)')
parser.add_argument('--max5mis', type=int, default=1, help='Maximum 5\' mismatches to trim. Reads with more than this number will be excluded.'
                                                           '(Default: 1)')
parser.add_argument('--regressfile', default='regression.h5',
                    help='Filename to which to output the table of regression scores for each ORF. Formatted as pandas HDF (tables generated include '
                         '"start_strengths", "orf_strengths", and "stop_strengths"). If SUBDIR is set, this file will be placed in that directory. '
                         '(Default: regression.h5)')
parser.add_argument('--startonly', action='store_true', help='Toggle for datasets collected in the presence of initiation inhibitor (e.g. HARR, '
                                                             'LTM). If selected, "stop_strengths" will not be calculated or saved.')
parser.add_argument('--startrange', type=int, nargs=2, default=[1, 50],
                    help='Region around start codon (in codons) to model explicitly. Ignored if reading metagene from file (Default: 1 50, meaning '
                         'one full codon before the start is modeled, as are the start codon and the 49 codons following it).')
parser.add_argument('--stoprange', type=int, nargs=2, default=[7, 0],
                    help='Region around stop codon (in codons) to model explicitly. Ignored if reading metagene from file (Default: 7 0, meaning '
                         'seven full codons before and including the stop are modeled, but none after).')
parser.add_argument('--mincdsreads', type=int, default=64,
                    help='Minimum number of reads required within the body of the CDS (and any surrounding nucleotides indicated by STARTRANGE or '
                         'STOPRANGE) for it to be included in the metagene. Ignored if reading metagene from file (Default: 64).')
parser.add_argument('--startcount', type=int, default=0,
                    help='Minimum reads at putative translation initiation codon. Useful to reduce computational burden by only considering ORFs '
                         'with e.g. at least 1 read at the start. (Default: 0)')
parser.add_argument('--metagenefile', default='metagene.txt',
                    help='File to save metagene profile, OR if the file already exists, it will be used as the input metagene. Formatted as '
                         'tab-delimited text, with position, readlength, value, and type ("START", "CDS", or "STOP"). If SUBDIR is set, this file '
                         'will be placed in that directory. (Default: metagene.txt)')
parser.add_argument('--noregress', action='store_true', help='Only generate a metagene (i.e. do not perform any regressions)')
parser.add_argument('-v', '--verbose', action='count', help='Output a log of progress and timing (to stdout). Repeat for higher verbosity level.')
parser.add_argument('-p', '--numproc', type=int, default=1, help='Number of processes to run. Defaults to 1 but more recommended if available.')
parser.add_argument('-f', '--force', action='store_true',
                    help='Force file overwrite. This will overwrite both METAGENEFILE and REGRESSFILE, if they exist. To overwrite only REGRESSFILE '
                         '(and not the METAGENEFILE), do not invoke this option but simply delete REGRESSFILE.')
opts = parser.parse_args()

offsetfilename = os.path.join(opts.subdir, opts.offsetfile)
metafilename = os.path.join(opts.subdir, opts.metagenefile)
regressfilename = os.path.join(opts.subdir, opts.regressfile)

if not opts.force:
    if os.path.exists(regressfilename):
        if os.path.exists(metafilename):
            raise IOError('%s exists; use --force to overwrite (will also recalculate metagene and overwrite %s)' % (regressfilename, metafilename))
        raise IOError('%s exists; use --force to overwrite' % regressfilename)

restrictbystartfilenames = []
if opts.restrictbystarts:
    if len(opts.restrictbystarts) > 1 and len(opts.minwstart) == 1:
        opts.minwstart *= len(opts.restrictbystarts)  # expand the list to the same number of arguments
    if len(opts.minwstart) != len(opts.restrictbystarts):
        raise ValueError('--minwstart must be given same number of values as --restrictbystarts, or one value for all')
    for restrictbystart in opts.restrictbystarts:
        if os.path.isfile(restrictbystart):
            restrictbystartfilenames.append(restrictbystart)
        elif os.path.isdir(restrictbystart) and os.path.isfile(os.path.join(restrictbystart, opts.regressfile)):
            restrictbystartfilenames.append(os.path.join(restrictbystart, opts.regressfile))
        else:
            raise IOError('Regression file/directory %s not found' % restrictbystart)

if opts.verbose:
    sys.stdout.write(' '.join(sys.argv) + '\n')

    def logprint(nextstr):
        sys.stdout.write('[%s] %s\n' % (strftime('%Y-%m-%d %H:%M:%S'), nextstr))
        sys.stdout.flush()

    log_lock = mp.Lock()

rdlens = []
Pdict = {}
with open(offsetfilename, 'rU') as infile:
    for line in infile:
        ls = line.strip().split()
        rdlen = int(ls[0])
        for nmis in range(opts.max5mis+1):
            Pdict[(rdlen, nmis)] = int(ls[1])+nmis  # e.g. if nmis == 1, offset as though the read were missing that base entirely
        rdlens.append(rdlen)
    # Pdict = {(int(ls[0]), nmis): int(ls[1])+nmis for ls in [line.strip().split() for line in infile] for nmis in range(opts.max5mis+1)}
    # Pdict = {(ls[0], nmis): ls[1] for ls in [line.strip().split() for line in infile] if opts.maxrdlen >= ls[0] >= opts.minrdlen
    #          for nmis in range(opts.max5mis+1)}
rdlens.sort()

# hash transcripts by ID for easy reference later
with open(opts.inbed, 'rU') as inbed:
    bedlinedict = {line.split()[3]: line for line in inbed}


def _get_annotated_counts_by_chrom(chrom_to_do):
    """Accumulate counts from annotated CDSs into a metagene profile. Only the longest CDS in each transcript family will be included, and only if it
    meets the minimum number-of-reads requirement. Reads are normalized by gene, so every gene included contributes equally to the final metagene."""
    found_cds = pd.read_hdf(opts.orfstore, 'all_orfs', mode='r',
                            where="chrom == '%s' and orftype == 'annotated' and tstop > 0 and tcoord > %d and AAlen > %d"
                                  % (chrom_to_do, -startnt[0], min_AAlen),
                            columns=['orfname', 'tfam', 'tid', 'tcoord', 'tstop', 'AAlen']) \
        .sort('AAlen', ascending=False).drop_duplicates('tfam')  # use the longest annotated CDS in each transcript family
    num_cds_incl = 0  # number of CDSs included from this chromosome
    startprof = np.zeros((len(rdlens), startlen))
    cdsprof = np.zeros((len(rdlens), 3))
    stopprof = np.zeros((len(rdlens), stoplen))
    inbams = [pysam.Samfile(infile, 'rb') for infile in opts.bamfiles]
    gnd = HashedReadBAMGenomeArray(inbams, ReadKeyMapFactory(Pdict, read_length_nmis))

    for (tid, tcoord, tstop) in found_cds[['tid', 'tcoord', 'tstop']].itertuples(False):
        curr_trans = SegmentChain.from_bed(bedlinedict[tid])
        tlen = curr_trans.get_length()
        if tlen >= tstop + stopnt[1]:  # need to guarantee that the 3' UTR is sufficiently long
            curr_hashed_counts = get_hashed_counts(curr_trans, gnd)
            cdslen = tstop+stopnt[1]-tcoord-startnt[0]  # cds length, plus the extra bases...
            curr_counts = np.zeros((len(rdlens), cdslen))
            for (i, rdlen) in enumerate(rdlens):
                for nmis in range(opts.max5mis+1):
                    curr_counts[i, :] += curr_hashed_counts[(rdlen, nmis)][tcoord+startnt[0]:tstop+stopnt[1]]
                    # curr_counts is limited to the CDS plus any extra requested nucleotides on either side
            if curr_counts.sum() >= opts.mincdsreads:
                curr_counts /= curr_counts.mean()  # normalize by mean of counts across all readlengths and positions within the CDS
                startprof += curr_counts[:, :startlen]
                cdsprof += curr_counts[:, startlen:cdslen-stoplen].reshape((len(rdlens), -1, 3)).mean(1)
                stopprof += curr_counts[:, cdslen-stoplen:cdslen]
                num_cds_incl += 1

    for inbam in inbams:
        inbam.close()

    return startprof, cdsprof, stopprof, num_cds_incl


def _orf_profile(orflen):
    """Generate a profile for an ORF based on the metagene profile
    Parameters
    ----------
    orflen : int
        Number of nucleotides in the ORF, including the start and stop codons

    Returns
    -------
    np.ndarray<float>
        The expected profile for the ORF. Number of rows will match the number of rows in the metagene profile. Number of columns will be
        orflen + stopnt[1] - startnt[0]
    """
    assert orflen % 3 == 0
    assert orflen > 0
    short_stop = 9
    if orflen >= startnt[1]-stopnt[0]:  # long enough to include everything
        return np.hstack((startprof, np.tile(cdsprof, (orflen-startnt[1]+stopnt[0])/3), stopprof))
    elif orflen >= startnt[1]+short_stop:
        return np.hstack((startprof, stopprof[:, startnt[1]-orflen-stopnt[1]:]))
    elif orflen >= short_stop:
        return np.hstack((startprof[:, :orflen-short_stop-startnt[0]], stopprof[:, -short_stop-stopnt[1]:]))
    else:  # very short!
        return np.hstack((startprof[:, :3-startnt[0]], stopprof[:, 3-orflen-stopnt[0]:]))


if opts.startonly:
    failure_return = (pd.DataFrame(), pd.DataFrame())
else:
    failure_return = (pd.DataFrame(), pd.DataFrame(), pd.DataFrame())


def _regress_tfam(orf_set, gnd):
    """Performs non-negative least squares regression on all of the ORFs in a transcript family, using profiles constructed via _orf_profile()
    Also calculates Wald statistics for each orf and start codon, and for each stop codon if opts.startonly is False"""
    tfam = orf_set['tfam'].iat[0]
    strand = orf_set['strand'].iat[0]
    chrom = orf_set['chrom'].iat[0]
    tids = orf_set['tid'].drop_duplicates().tolist()
    all_tfam_genpos = set()
    tid_genpos = {}
    tlens = {}
    for (i, tid) in enumerate(tids):
        currtrans = SegmentChain.from_bed(bedlinedict[tid])
        curr_pos_set = currtrans.get_position_set()
        tlens[tid] = len(curr_pos_set)
        tid_genpos[tid] = curr_pos_set
        all_tfam_genpos.update(curr_pos_set)
    tfam_segs = SegmentChain(*positionlist_to_segments(chrom, strand, list(all_tfam_genpos)))
    all_tfam_genpos = np.array(sorted(all_tfam_genpos))
    if strand == '-':
        all_tfam_genpos = all_tfam_genpos[::-1]
    nnt = len(all_tfam_genpos)
    tid_indices = {tid: np.flatnonzero(np.in1d(all_tfam_genpos, list(curr_tid_genpos), assume_unique=True))
                   for (tid, curr_tid_genpos) in tid_genpos.iteritems()}
    hashed_counts = get_hashed_counts(tfam_segs, gnd)
    counts = np.zeros((len(rdlens), nnt), dtype=np.float64)  # even though they are integer-valued, will need to do float arithmetic
    for (i, rdlen) in enumerate(rdlens):
        for nmis in range(1+opts.max5mis):
            counts[i, :] += hashed_counts[(rdlen, nmis)]
    counts = counts.ravel()

    if opts.startcount:
        # Only include ORFS for which there is at least some minimum reads within one nucleotide of the start codon
        offsetmat = np.tile(nnt*np.arange(len(rdlens)), 3)  # offsets for each cond, expecting three positions to check for each
    #    try:
        orf_set = orf_set[[(counts[(start_idxes.repeat(len(rdlens))+offsetmat)].sum() >= opts.startcount) for start_idxes in
                           [tid_indices[tid][tcoord-1:tcoord+2] for (tid, tcoord, tstop) in orf_set[['tid', 'tcoord', 'tstop']].itertuples(False)]]]
        if orf_set.empty:
            return failure_return

    orf_strength_df = orf_set.sort('tcoord', ascending=False).drop_duplicates('orfname').reset_index(drop=True)
    abort_set = orf_set.drop_duplicates('gcoord').copy()
    abort_set['gstop'] = abort_set['gcoord']  # should maybe be +/-3, but then need to worry about splicing - and this is an easy flag
    abort_set['tstop'] = abort_set['tcoord']+3  # stop after the first codon
    abort_set['orfname'] = abort_set['gcoord'].apply(lambda x: '%s_%d_abort' % (tfam, x))
    orf_strength_df = pd.concat((orf_strength_df, abort_set), ignore_index=True)
    if not opts.startonly:  # if marking full ORFs, include histop model
        stop_set = orf_set.drop_duplicates('gstop').copy()
        stop_set['gcoord'] = stop_set['gstop']  # this is an easy flag
        stop_set['tcoord'] = stop_set['tstop']  # should probably be -3 nt, but this is another easy flag that distinguishes from abinit
        stop_set['orfname'] = stop_set['gstop'].apply(lambda x: '%s_%d_stop' % (tfam, x))
        orf_strength_df = pd.concat((orf_strength_df, stop_set), ignore_index=True)
    orf_profs = []
    indices = []
    for (tid, tcoord, tstop) in orf_strength_df[['tid', 'tcoord', 'tstop']].itertuples(False):
        if tcoord != tstop:  # not a histop
            tlen = tlens[tid]
            if tcoord+startnt[0] < 0:
                startadj = -startnt[0]-tcoord  # number of nts to remove from the start due to short 5' UTR; guaranteed > 0
            else:
                startadj = 0
            if tstop+stopnt[1] > tlen:
                stopadj = tstop+stopnt[1]-tlen  # number of nts to remove from the end due to short 3' UTR; guaranteed > 0
            else:
                stopadj = 0
            curr_indices = tid_indices[tid][tcoord+startnt[0]+startadj:tstop+stopnt[1]-stopadj]
            orf_profs.append(_orf_profile(tstop-tcoord)[:, startadj:tstop-tcoord+stopnt[1]-startnt[0]-stopadj].ravel())
        else:  # histop
            curr_indices = tid_indices[tid][tstop-6:tstop]
            orf_profs.append(stopprof[:, -6:].ravel())
        indices.append(np.concatenate([nnt*i+curr_indices for i in xrange(len(rdlens))]))
        # need to tile the indices for each read length
        if len(indices[-1]) != len(orf_profs[-1]):
            raise AssertionError('ORF length does not match index length')
    orf_matrix = scipy.sparse.csc_matrix((np.concatenate(orf_profs),
                                          np.concatenate(indices),
                                          np.cumsum([0]+[len(curr_indices) for curr_indices in indices])),
                                         shape=(nnt*len(rdlens), len(orf_strength_df)))
    # better to make it a sparse matrix, even though nnls requires a dense matrix, because of linear algebra to come
    nonzero_orfs = np.flatnonzero(orf_matrix.T.dot(counts) > 0)
    if len(nonzero_orfs) == 0:  # no possibility of anything coming up
        return failure_return
    orf_matrix = orf_matrix[:, nonzero_orfs]
    orf_strength_df = orf_strength_df.iloc[nonzero_orfs]  # don't bother fitting ORFs with zero reads throughout their entire length
    (orf_strs, resid) = nnls(orf_matrix.toarray(), counts)
    min_str = 1e-6  # allow for machine rounding error
    usable_orfs = orf_strs > min_str
    if not usable_orfs.any():
        return failure_return
    orf_strength_df = orf_strength_df[usable_orfs]
    orf_matrix = orf_matrix[:, usable_orfs] # remove entries for zero-strength ORFs or transcripts
    orf_strs = orf_strs[usable_orfs]
    orf_strength_df['orf_strength'] = orf_strs

    covmat = resid*resid*np.linalg.inv(orf_matrix.T.dot(orf_matrix).toarray())/(nnt*len(rdlens)-len(orf_strength_df))
    # homoscedastic version (assume equal variance at all positions)

    # resids = counts-orf_matrix.dot(orf_strs)
    # simple_covmat = np.linalg.inv(orf_matrix.T.dot(orf_matrix).toarray())
    # covmat = simple_covmat.dot(orf_matrix.T.dot(scipy.sparse.dia_matrix((resids*resids, 0), (len(resids), len(resids))))
    #                            .dot(orf_matrix).dot(simple_covmat))
    # # heteroscedastic version (Eicker-Huber-White robust estimator)

    orf_strength_df['W_orf'] = orf_strength_df['orf_strength']*orf_strength_df['orf_strength']/np.diag(covmat)
    orf_strength_df.set_index('orfname', inplace=True)
    elongating_orfs = ~(orf_strength_df['gstop'] == orf_strength_df['gcoord'])
    if opts.startonly:  # count abortive initiation events towards start strength in this case
        include_starts = (orf_strength_df['tcoord'] != orf_strength_df['tstop'])
        gcoord_grps = orf_strength_df[include_starts].groupby('gcoord')
        # even if we are willing to count abinit towards start strength, we certainly shouldn't count histop
        covmat_starts = covmat[np.ix_(include_starts.values, include_starts.values)]
        orf_strs_starts = orf_strs[include_starts.values]
    else:
        gcoord_grps = orf_strength_df[elongating_orfs].groupby('gcoord')
        covmat_starts = covmat[np.ix_(elongating_orfs.values, elongating_orfs.values)]
        orf_strs_starts = orf_strs[elongating_orfs.values]
    start_strength_df = pd.DataFrame.from_items([('tfam', tfam),
                                                 ('chrom', orf_set['chrom'].iloc[0]),
                                                 ('strand', orf_set['strand'].iloc[0]),
                                                 ('codon', gcoord_grps['codon'].first()),
                                                 ('start_strength', gcoord_grps['orf_strength'].aggregate(np.sum))])
    start_strength_df['W_start'] = pd.Series({gcoord: orf_strs_starts[rownums].dot(np.linalg.inv(covmat_starts[np.ix_(rownums, rownums)]))
                                              .dot(orf_strs_starts[rownums]) for (gcoord, rownums) in gcoord_grps.indices.iteritems()})

    if not opts.startonly:
        # count histop towards the stop codon - but still exclude abinit
        include_stops = (elongating_orfs | (orf_strength_df['tcoord'] == orf_strength_df['tstop']))
        gstop_grps = orf_strength_df[include_stops].groupby('gstop')
        covmat_stops = covmat[np.ix_(include_stops.values, include_stops.values)]
        orf_strs_stops = orf_strs[include_stops.values]
        stop_strength_df = pd.DataFrame.from_items([('tfam', tfam),
                                                    ('chrom', orf_set['chrom'].iloc[0]),
                                                    ('strand', orf_set['strand'].iloc[0]),
                                                    ('stop_strength', gstop_grps['orf_strength'].aggregate(np.sum))])
        stop_strength_df['W_stop'] = pd.Series({gstop: orf_strs_stops[rownums].dot(np.linalg.inv(covmat_stops[np.ix_(rownums, rownums)]))
                                                .dot(orf_strs_stops[rownums]) for (gstop, rownums) in gstop_grps.indices.iteritems()})

        # # nohistop
        # gstop_grps = orf_strength_df[elongating_orfs].groupby('gstop')
        # covmat_stops = covmat[np.ix_(elongating_orfs.values, elongating_orfs.values)]
        # orf_strs_stops = orf_strs[elongating_orfs.values]
        # stop_strength_df['stop_strength_nohistop'] = gstop_grps['orf_strength'].aggregate(np.sum)
        # stop_strength_df['W_stop_nohistop'] = pd.Series({gstop:orf_strs_stops[rownums].dot(np.linalg.inv(covmat_stops[np.ix_(rownums,rownums)]))
        #                                                  .dot(orf_strs_stops[rownums]) for (gstop, rownums) in gstop_grps.indices.iteritems()})

        return orf_strength_df, start_strength_df, stop_strength_df
    else:
        return orf_strength_df, start_strength_df


def _regress_chrom(chrom_to_do):
    """Applies _regress_tfam() to all of the transcript families on a chromosome"""
    chrom_orfs = pd.read_hdf(opts.orfstore, 'all_orfs', mode='r', where="chrom == %r and tstop > 0 and tcoord > 0" % chrom_to_do,
                             columns=['orfname', 'tfam', 'tid', 'tcoord', 'tstop', 'AAlen', 'chrom', 'gcoord', 'gstop', 'strand',
                                      'codon', 'orftype', 'annot_start', 'annot_stop'])
    # tcoord > 0 removes ORFs where the first codon is an NTG, to avoid an indexing error
    # Those ORFs would never get called anyway since they couldn't possibly have any reads at their start codon

    if restrictbystartfilenames:
        restrictedstarts = pd.DataFrame()
        for (restrictbystart, minw) in zip(restrictbystartfilenames, opts.minwstart):
            restrictedstarts = restrictedstarts.append(
                pd.read_hdf(restrictbystart, 'start_strengths', mode='r', where="(chrom == %r) & (W_start > minw)" % chrom_to_do,
                            columns=['tfam', 'chrom', 'gcoord', 'strand']), ignore_index=True).drop_duplicates()
        chrom_orfs = chrom_orfs.merge(restrictedstarts)  # inner merge acts as a filter

    if chrom_orfs.empty:
        if opts.verbose > 1:
            logprint('No ORFs found on %s' % chrom_to_do)
        return failure_return

    inbams = [pysam.Samfile(infile, 'rb') for infile in opts.bamfiles]
    gnd = HashedReadBAMGenomeArray(inbams, ReadKeyMapFactory(Pdict, read_length_nmis))

    res = tuple([pd.concat(res_dfs) for res_dfs in zip(*[_regress_tfam(tfam_set, gnd) for (tfam, tfam_set) in chrom_orfs.groupby('tfam')])])

    for inbam in inbams:
        inbam.close()

    if opts.verbose > 1:
        logprint('%s complete' % chrom_to_do)

    return res

with pd.get_store(opts.orfstore, mode='r') as orfstore:
    chroms = orfstore.select('all_orfs/meta/chrom/meta').values  # because saved as categorical, this is the list of all chromosomes

if os.path.isfile(metafilename) and not opts.force:
    if opts.verbose:
        logprint('Loading metagene')

    metagene = pd.read_csv(metafilename, sep='\t').set_index(['region', 'position'])
    metagene.columns = metagene.columns.astype(int)  # they are read lengths
    assert (metagene.columns == rdlens).all()
    startprof = metagene.loc['START']
    cdsprof = metagene.loc['CDS']
    stopprof = metagene.loc['STOP']
    startnt = (startprof.index.min(), startprof.index.max()+1)
    assert len(cdsprof) == 3
    stopnt = (stopprof.index.min(), stopprof.index.max()+1)
    startprof = startprof.values.T
    cdsprof = cdsprof.values.T
    stopprof = stopprof.values.T
else:
    if opts.verbose:
        logprint('Calculating metagene')

    startnt = (-abs(opts.startrange[0])*3, abs(opts.startrange[1])*3)  # force <=0 and >= 0 for the bounds
    stopnt = (-abs(opts.stoprange[0])*3, abs(opts.stoprange[1])*3)

    if stopnt[0] >= -6:
        raise ValueError('STOPRANGE must encompass at least 3 codons prior to the stop')
    min_AAlen = (startnt[1]-stopnt[0])/3  # actually should be longer than this to ensure at least one codon in the body
    startlen = startnt[1]-startnt[0]
    stoplen = stopnt[1]-stopnt[0]

    workers = mp.Pool(opts.numproc)
    (startprof, cdsprof, stopprof, num_cds_incl) = [sum(x) for x in zip(*workers.map(_get_annotated_counts_by_chrom, chroms))]
    workers.close()

    startprof /= num_cds_incl  # technically not necessary, but helps for consistency of units across samples
    cdsprof /= num_cds_incl
    stopprof /= num_cds_incl

    pd.concat((pd.DataFrame(data=startprof.T,
                            index=pd.MultiIndex.from_product(['START', np.arange(*startnt)], names=['region', 'position']),
                            columns=pd.Index(rdlens, name='rdlen')),
               pd.DataFrame(data=cdsprof.T,
                            index=pd.MultiIndex.from_product(['CDS', np.arange(3)], names=['region', 'position']),
                            columns=pd.Index(rdlens, name='rdlen')),
               pd.DataFrame(data=stopprof.T,
                            index=pd.MultiIndex.from_product(['STOP', np.arange(*stopnt)], names=['region', 'position']),
                            columns=pd.Index(rdlens, name='rdlen')))) \
        .to_csv(metafilename, sep='\t')

catfields = ['chrom', 'strand', 'codon', 'orftype']

if not opts.noregress:
    if opts.verbose:
        logprint('Calculating regression results by chromosome')
    workers = mp.Pool(opts.numproc)
    if opts.startonly:
        (orf_strengths, start_strengths) = \
            [pd.concat(res_dfs).reset_index() for res_dfs in zip(*workers.map(_regress_chrom, chroms))]
        if opts.verbose:
            logprint('Saving results')
        for catfield in catfields:
            if catfield in start_strengths.columns:
                start_strengths[catfield] = start_strengths[catfield].astype('category')  # saves disk space and read/write time
            if catfield in orf_strengths.columns:
                orf_strengths[catfield] = orf_strengths[catfield].astype('category')  # saves disk space and read/write time
        with pd.get_store(regressfilename, mode='w') as outstore:
            outstore.put('orf_strengths', orf_strengths, format='t', data_columns=True)
            outstore.put('start_strengths', start_strengths, format='t', data_columns=True)
    else:
        (orf_strengths, start_strengths, stop_strengths) = \
            [pd.concat(res_dfs).reset_index() for res_dfs in zip(*workers.map(_regress_chrom, chroms))]
        if opts.verbose:
            logprint('Saving results')
        for catfield in catfields:
            if catfield in start_strengths.columns:
                start_strengths[catfield] = start_strengths[catfield].astype('category')  # saves disk space and read/write time
            if catfield in orf_strengths.columns:
                orf_strengths[catfield] = orf_strengths[catfield].astype('category')  # saves disk space and read/write time
            if catfield in stop_strengths.columns:
                stop_strengths[catfield] = stop_strengths[catfield].astype('category')  # saves disk space and read/write time
        with pd.get_store(regressfilename, mode='w') as outstore:
            outstore.put('orf_strengths', orf_strengths, format='t', data_columns=True)
            outstore.put('start_strengths', start_strengths, format='t', data_columns=True)
            outstore.put('stop_strengths', stop_strengths, format='t', data_columns=True)
    workers.close()

if opts.verbose:
    logprint('Tasks complete')
