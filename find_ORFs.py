#! /usr/bin/env python

import argparse
from Bio import SeqIO
from yeti.genomics.seqtools import seq_to_regex, IUPAC_TABLE_DNA
from yeti.genomics.roitools import Transcript, SegmentChain
import re
from collections import defaultdict
import pandas as pd
import numpy as np
import multiprocessing as mp
import subprocess as sp
import os
import sys
from time import strftime

parser = argparse.ArgumentParser(description='Identify all possible ORFs in a transcriptome. ORF-RATER will evaluate translation of only these ORFs.')
parser.add_argument('genomefasta', help='Path to genome FASTA-file')
parser.add_argument('--tfamstem', default='tfams', help='Transcript family information generated by make_tfams.py. Both TFAMSTEM.txt and '
                                                        'TFAMSTEM.bed should exist. (Default: tfams)')
parser.add_argument('--orfstore', default='orf.h5',
                    help='File to which to output the final table of identified ORFs. Will be formatted as a pandas HDF store (table name is '
                         '"all_ORFs"). Different columns of the table indicate various of each ORF, such as start codon, length, etc. '
                         '(Default: orf.h5)')
parser.add_argument('--inbed', default='transcripts.bed', help='Transcriptome BED-file (Default: transcripts.bed)')
parser.add_argument('--codons', nargs='+', default=['ATG'],
                    help='Codons to consider as possible translation initiation sites. All must be 3 nucleotides long. Standard IUPAC nucleotide '
                         'codes are recognized; for example, to query all NTG codons, one could input "NTG" or "ATG CTG GTG TTG" (Default: ATG)')
parser.add_argument('-v', '--verbose', action='store_true', help='Output a log of progress and timing (to stdout)')
parser.add_argument('-p', '--numproc', type=int, default=1, help='Number of processes to run. Defaults to 1 but recommended to use more (e.g. 12-16)')
parser.add_argument('-f', '--force', action='store_true', help='Force file overwrite')
opts = parser.parse_args()

if not opts.force and os.path.exists(opts.orfstore):
    raise IOError('%s exists; use --force to overwrite' % opts.orfstore)

for codon in opts.codons:
    if len(codon) != 3 or any(x not in IUPAC_TABLE_DNA for x in codon.upper()):
        raise ValueError('%s is an invalid codon sequence' % codon)

if opts.verbose:
    sys.stdout.write(' '.join(sys.argv) + '\n')

    def logprint(nextstr):
        sys.stdout.write('[%s] %s\n' % (strftime('%Y-%m-%d %H:%M:%S'), nextstr))
        sys.stdout.flush()

    logprint('Reading transcriptome and genome')

START_RE = seq_to_regex('|'.join(opts.codons), nucleotide_table=IUPAC_TABLE_DNA)
STOP_RE = re.compile(r'(?:...)*?(?:TAG|TAA|TGA)')

# hash transcripts by ID for easy reference later
with open(opts.inbed, 'rU') as inbed:
    bedlinedict = {line.split()[3]: line for line in inbed}

tfamtids = defaultdict(list)
with open('%s.txt' % opts.tfamstem, 'rU') as tfamtable:
    for line in tfamtable:
        ls = line.strip().split()
        tfamtids[ls[1]].append(ls[0])

with open('%s.bed' % opts.tfamstem, 'rU') as tfambed:
    tfambedlines = {line.split()[3]: line for line in tfambed}

genome = SeqIO.to_dict(SeqIO.parse(opts.genomefasta, 'fasta'))


def find_all_ORFs(myseq):
    """Identify ORFs, or at least starts.
    Returns list of (start,stop,codon), where stop == 0 if no valid stop codon is present.
    Starts are NTGs
    """
    result = []
    for i in range(len(myseq)-2):
        if START_RE.match(myseq[i:i+3]):
            m = STOP_RE.match(myseq[i:])
            if m:
                result.append((i, m.end()+i, myseq[i:i+3]))
            else:
                result.append((i, 0, myseq[i:i+3]))
    return result


def name_ORF(tfam, gcoord, AAlen):
    return '%s_%d_%daa' % (tfam, gcoord, AAlen)


def identify_tfam_ORFs((tfam, tids)):
    currtfam = SegmentChain.from_bed(tfambedlines[tfam])
    chrom = currtfam.chrom
    strand = currtfam.strand
    tfam_genpos = np.array(currtfam.get_position_list(stranded=True))
    tmask = np.empty((len(tids), len(tfam_genpos)), dtype=np.bool)  # True if transcript covers that position, False if not
    tfam_dfs = []
    tidx_lookup = {}
    for tidx, tid in enumerate(tids):
        tidx_lookup[tid] = tidx
        curr_trans = Transcript.from_bed(bedlinedict[tid])
        tmask[tidx, :] = np.in1d(tfam_genpos, curr_trans.get_position_list(stranded=True), assume_unique=True)
        trans_ORF = find_all_ORFs(curr_trans.get_sequence(genome).upper())
        if trans_ORF:
            (startpos, stoppos, codons) = zip(*trans_ORF)
            startpos = np.array(startpos)
            stoppos = np.array(stoppos)

            gcoords = np.array(curr_trans.get_genomic_coordinate(startpos)[1], dtype='u4')

            stop_present = (stoppos > 0)
            gstops = np.zeros(len(trans_ORF), dtype='u4')
            gstops[stop_present] = curr_trans.get_genomic_coordinate(stoppos[stop_present] - 1)[1] + (strand == '+')*2 - 1
            # the decrementing/incrementing stuff preserves half-openness regardless of strand

            AAlens = np.zeros(len(trans_ORF), dtype='u4')
            AAlens[stop_present] = (stoppos[stop_present] - startpos[stop_present])/3 - 1
            tfam_dfs.append(pd.DataFrame.from_items([('tfam', tfam),
                                                     ('tid', tid),
                                                     ('tcoord', startpos),
                                                     ('tstop', stoppos),
                                                     ('chrom', chrom),
                                                     ('gcoord', gcoords),
                                                     ('gstop', gstops),
                                                     ('strand', strand),
                                                     ('codon', codons),
                                                     ('AAlen', AAlens),
                                                     ('ORF_name', '')]))
    if any(x is not None for x in tfam_dfs):
        tfam_dfs = pd.concat(tfam_dfs, ignore_index=True)
        for ((gcoord, AAlen), gcoord_grp) in tfam_dfs.groupby(['gcoord', 'AAlen']):  # group by genomic start position and length
            if len(gcoord_grp) == 1:
                tfam_dfs.loc[gcoord_grp.index, 'ORF_name'] = name_ORF(tfam, gcoord, AAlen)
            else:
                ORF_gcoords = np.vstack(np.flatnonzero(tmask[tidx_lookup[tid], :])[tcoord:tstop]
                                        for (tid, tcoord, tstop) in gcoord_grp[['tid', 'tcoord', 'tstop']].itertuples(False))
                if (ORF_gcoords == ORF_gcoords[0, :]).all():  # all of the grouped ORFs are identical, so should receive the same name
                    tfam_dfs.loc[gcoord_grp.index, 'ORF_name'] = name_ORF(tfam, gcoord, AAlen)
                else:
                    named_so_far = 0
                    unnamed = np.ones(len(gcoord_grp), dtype=np.bool)
                    basename = name_ORF(tfam, gcoord, AAlen)
                    while unnamed.any():
                        identicals = (ORF_gcoords == ORF_gcoords[unnamed, :][0, :]).all(1)
                        tfam_dfs.loc[gcoord_grp.index[identicals], 'ORF_name'] = '%s_%d' % (basename, named_so_far)
                        unnamed[identicals] = False
                        named_so_far += 1
        return tfam_dfs
    else:
        return None

if opts.verbose:
    logprint('Identifying ORFs within each transcript family')

workers = mp.Pool(opts.numproc)
all_ORFs = pd.concat(workers.map(identify_tfam_ORFs, tfamtids.iteritems()), ignore_index=True)
workers.close()

for catfield in ['chrom', 'strand', 'codon']:
    all_ORFs[catfield] = all_ORFs[catfield].astype('category')  # saves disk space and read/write time

if opts.verbose:
    logprint('Saving results')

origname = opts.orfstore+'.tmp'
all_ORFs.to_hdf(origname, 'all_ORFs', format='t', data_columns=True)
sp.call(['ptrepack', origname, opts.orfstore])  # repack for efficiency
os.remove(origname)

if opts.verbose:
    logprint('Tasks complete')
