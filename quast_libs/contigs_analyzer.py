############################################################################
# Copyright (c) 2015-2018 Saint Petersburg State University
# Copyright (c) 2011-2015 Saint Petersburg Academic University
# All Rights Reserved
# See file LICENSE for details.
############################################################################
#
# Some comments in this script was kindly provided by Plantagora team and
# modified by QUAST team. It is not licensed under GPL as other parts of QUAST,
# but it can be distributed and used in QUAST pipeline with current remarks and
# citation. For more details about assess_assembly.pl please refer to
# http://www.plantagora.org website and to the following paper:
#
# Barthelson R, McFarlin AJ, Rounsley SD, Young S (2011) Plantagora: Modeling
# Whole Genome Sequencing and Assembly of Plant Genomes. PLoS ONE 6(12):
# e28436. doi:10.1371/journal.pone.0028436
############################################################################

from __future__ import with_statement
import os
import sys
import re
from collections import defaultdict
from os.path import join, dirname

from quast_libs import reporting, qconfig, qutils, fastaparser, N50
from quast_libs.ca_utils import misc
from quast_libs.ca_utils.analyze_contigs import analyze_contigs
from quast_libs.ca_utils.analyze_misassemblies import Mapping, IndelsInfo
from quast_libs.ca_utils.misc import ref_labels_by_chromosomes, compile_aligner, \
    create_minimap_output_dir, close_handlers, parse_cs_tag

from quast_libs.ca_utils.align_contigs import align_contigs, get_aux_out_fpaths, AlignerStatus
from quast_libs.ca_utils.save_results import print_results, save_result, save_result_for_unaligned, \
    save_combined_ref_stats
from quast_libs.fastaparser import get_genome_stats

from quast_libs.log import get_logger
from quast_libs.qutils import is_python2, run_parallel

logger = get_logger(qconfig.LOGGER_DEFAULT_NAME)


class CAOutput():
    def __init__(self, stdout_f, misassembly_f=None, coords_filtered_f=None, used_snps_f=None, icarus_out_f=None):
        self.stdout_f = stdout_f
        self.misassembly_f = misassembly_f
        self.coords_filtered_f = coords_filtered_f
        self.used_snps_f = used_snps_f
        self.icarus_out_f = icarus_out_f

from pprint import pprint

def filter_count(list, filter):
    count = 0
    for item in list:
        if filter(item):
            count += 1
    return count

def P(list, minimum):
    print("      Computing P{}".format(minimum))
    return filter_count(list, lambda x: x >= minimum) / len(list)

def analyze_coverage(ref_aligns, reference_chromosomes, ns_by_chromosomes, used_snps_fpath, contig_length_map=None):
    logger.info("    Enter analyze_coverage")
    #logger.info(f"    {ref_aligns=}")
    indels_info = IndelsInfo()
    maximum_contig_align_size_per_ref_base = {}
    strict_maximum_contig_align_size_per_ref_base = {}
    maximum_contig_length_per_ref_base = {}
    genome_mapping = {}
    genome_length = 0
    for chr_name, chr_len in reference_chromosomes.items():
        logger.info(f"      Chromosome {chr_name} has length {chr_len}")
        genome_mapping[chr_name] = [0] * (chr_len + 1)
        maximum_contig_align_size_per_ref_base[chr_name] = [0] * (chr_len + 1)
        strict_maximum_contig_align_size_per_ref_base[chr_name] = [0] * (chr_len + 1)
        maximum_contig_length_per_ref_base[chr_name] = [0] * (chr_len + 1)
        genome_length += chr_len
    logger.info("      Genome length: " + str(genome_length))

    alignment_total_length = 0
    with open(used_snps_fpath, 'w') as used_snps_f:
        for chr_name, aligns in ref_aligns.items():
            for align in aligns:
                # Vars with 1 are on the reference, vars with 2 are on the contig
                ref_pos, ctg_pos = align.s1, align.s2
                strand_direction = 1 if align.s2 < align.e2 else -1
                for op in parse_cs_tag(align.cigar):
                    if op.startswith(':'):
                        n_bases = int(op[1:])
                    else:
                        n_bases = len(op) - 1
                    if op.startswith('*'):
                        ref_nucl, ctg_nucl = op[1].upper(), op[2].upper()
                        if ctg_nucl != 'N' and ref_nucl != 'N':
                            indels_info.mismatches += 1
                            if qconfig.show_snps:
                                used_snps_f.write('%s\t%s\t%d\t%s\t%s\t%d\n' % (chr_name, align.contig, ref_pos, ref_nucl, ctg_nucl, ctg_pos))
                        ref_pos += 1
                        ctg_pos += 1 * strand_direction
                    elif op.startswith('+'):
                        indels_info.indels_list.append(n_bases)
                        indels_info.insertions += n_bases
                        if qconfig.show_snps and n_bases < qconfig.MAX_INDEL_LENGTH:
                            ref_nucl, ctg_nucl = '.', op[1:].upper()
                            used_snps_f.write('%s\t%s\t%d\t%s\t%s\t%d\n' % (chr_name, align.contig, ref_pos, ref_nucl, ctg_nucl, ctg_pos))
                        ctg_pos += n_bases * strand_direction
                    elif op.startswith('-'):
                        indels_info.indels_list.append(n_bases)
                        indels_info.deletions += n_bases
                        if qconfig.show_snps and n_bases < qconfig.MAX_INDEL_LENGTH:
                            ref_nucl, ctg_nucl = op[1:].upper(), '.'
                            used_snps_f.write('%s\t%s\t%d\t%s\t%s\t%d\n' % (chr_name, align.contig, ref_pos, ref_nucl, ctg_nucl, ctg_pos))
                        ref_pos += n_bases
                    else:
                        ref_pos += n_bases
                        ctg_pos += n_bases * strand_direction

                alignment_total_length += align.len2_excluding_local_misassemblies
                align_size = align.len2_excluding_local_misassemblies # Use the same len that is used to compute NGAx
                strict_align_size = align.len2_including_local_misassemblies # Use the same len that is used to strict compute NGAx
                if contig_length_map is not None:
                    contig_length = contig_length_map[align.contig]
                else:
                    contig_length = 0

                if align.s1 < align.e1:
                    for pos in range(align.s1, align.e1):
                        assert pos >= 0
                        if pos >= len(genome_mapping[align.ref]):
                            logger.info(f"WARN alignment out of ref bounds! contig: {align.contig}; ref: {align.ref}; bounds: [0, {len(genome_mapping[align.ref])}]; alignment: [{align.s1}, {align.e1}]")
                            continue
                        genome_mapping[align.ref][pos] = 1
                        maximum_contig_align_size_per_ref_base[align.ref][pos] = max(align_size, maximum_contig_align_size_per_ref_base[align.ref][pos])
                        strict_maximum_contig_align_size_per_ref_base[align.ref][pos] = max(strict_align_size, strict_maximum_contig_align_size_per_ref_base[align.ref][pos])
                        maximum_contig_length_per_ref_base[align.ref][pos] = max(contig_length, maximum_contig_length_per_ref_base[align.ref][pos])
                else:
                    for pos in range(align.s1, len(genome_mapping[align.ref])):
                        assert pos >= 0
                        assert pos < len(genome_mapping[align.ref])
                        genome_mapping[align.ref][pos] = 1
                        maximum_contig_align_size_per_ref_base[align.ref][pos] = max(align_size, maximum_contig_align_size_per_ref_base[align.ref][pos])
                        strict_maximum_contig_align_size_per_ref_base[align.ref][pos] = max(strict_align_size, strict_maximum_contig_align_size_per_ref_base[align.ref][pos])
                        maximum_contig_length_per_ref_base[align.ref][pos] = max(contig_length, maximum_contig_length_per_ref_base[align.ref][pos])
                    for pos in range(0, align.e1):
                        assert pos >= 0
                        if pos >= len(genome_mapping[align.ref]):
                            logger.info(f"WARN alignment out of ref bounds! contig: {align.contig}; ref: {align.ref}; bounds: [0, {len(genome_mapping[align.ref])}]; alignment: [0, {align.e1}]")
                            continue
                        genome_mapping[align.ref][pos] = 1
                        maximum_contig_align_size_per_ref_base[align.ref][pos] = max(align_size, maximum_contig_align_size_per_ref_base[align.ref][pos])
                        strict_maximum_contig_align_size_per_ref_base[align.ref][pos] = max(strict_align_size, strict_maximum_contig_align_size_per_ref_base[align.ref][pos])
                        maximum_contig_length_per_ref_base[align.ref][pos] = max(contig_length, maximum_contig_length_per_ref_base[align.ref][pos])
            for pos in ns_by_chromosomes[align.ref]:
                genome_mapping[align.ref][pos] = 0
                maximum_contig_align_size_per_ref_base[align.ref][pos] = 0
                strict_maximum_contig_align_size_per_ref_base[align.ref][pos] = 0
                maximum_contig_length_per_ref_base[align.ref][pos] = 0

    covered_bases = sum([sum(genome_mapping[chrom]) for chrom in genome_mapping])
    if covered_bases == 0:
        logger.warning(f"      Found no covered bases, setting it to one anyways to prevent division by zero.")
        covered_bases = 1

    maximum_contig_align_size_per_ref_base = [align_size for contig in maximum_contig_align_size_per_ref_base.values() for align_size in contig]
    maximum_contig_align_size_per_ref_base.sort(reverse=True)
    ea_x_max = [0] * 101
    for i in range(0, 100):
        ea_x_max[i] = maximum_contig_align_size_per_ref_base[(len(maximum_contig_align_size_per_ref_base) * i) // 100]
    ea_x_max[100] = maximum_contig_align_size_per_ref_base[-1]
    ea_mean_max = int(sum(maximum_contig_align_size_per_ref_base) / len(maximum_contig_align_size_per_ref_base))
    p5k = P(maximum_contig_align_size_per_ref_base, 5000)
    p10k = P(maximum_contig_align_size_per_ref_base, 10000)
    p15k = P(maximum_contig_align_size_per_ref_base, 15000)
    p20k = P(maximum_contig_align_size_per_ref_base, 20000)

    strict_maximum_contig_align_size_per_ref_base = [align_size for contig in strict_maximum_contig_align_size_per_ref_base.values() for align_size in contig]
    strict_maximum_contig_align_size_per_ref_base.sort(reverse=True)
    strict_ea_x_max = [0] * 101
    for i in range(0, 100):
        strict_ea_x_max[i] = strict_maximum_contig_align_size_per_ref_base[(len(strict_maximum_contig_align_size_per_ref_base) * i) // 100]
    strict_ea_x_max[100] = strict_maximum_contig_align_size_per_ref_base[-1]
    strict_ea_mean_max = int(sum(strict_maximum_contig_align_size_per_ref_base) / len(strict_maximum_contig_align_size_per_ref_base))
    strict_p5k = P(strict_maximum_contig_align_size_per_ref_base, 5000)
    strict_p10k = P(strict_maximum_contig_align_size_per_ref_base, 10000)
    strict_p15k = P(strict_maximum_contig_align_size_per_ref_base, 15000)
    strict_p20k = P(strict_maximum_contig_align_size_per_ref_base, 20000)

    maximum_contig_length_per_ref_base = [length for contig in maximum_contig_length_per_ref_base.values() for length in contig]
    maximum_contig_length_per_ref_base.sort(reverse=True)
    e_x_max = [0] * 101
    for i in range(0, 100):
        e_x_max[i] = maximum_contig_length_per_ref_base[(len(maximum_contig_length_per_ref_base) * i) // 100]
    e_x_max[100] = maximum_contig_length_per_ref_base[-1]
    e_mean_max = int(sum(maximum_contig_length_per_ref_base) / len(maximum_contig_length_per_ref_base))

    #print("computed ea_x_max as " + str(ea_x_max))
    logger.info("      Duplication ratio = %.2f = %d/%d" % ((alignment_total_length / covered_bases), alignment_total_length, covered_bases))
    logger.info("      EA50max = {}".format(ea_x_max[50]))
    logger.info("      Strict EA50max = {}".format(strict_ea_x_max[50]))
    logger.info("      E50max = {}".format(e_x_max[50]))
    logger.info("      len2 NGA50 = {}".format(N50.NG50_and_LG50([align.len2 for aligns in ref_aligns.values() for align in aligns], genome_length, need_sort=True)[0]))
    logger.info("      len2_excluding_local_misassemblies NGA50 = {}".format(N50.NG50_and_LG50([align.len2_excluding_local_misassemblies for aligns in ref_aligns.values() for align in aligns], genome_length, need_sort=True)[0]))

    return covered_bases, indels_info, ea_x_max, strict_ea_x_max, ea_mean_max, strict_ea_mean_max, p5k, p10k, p15k, p20k, strict_p5k, strict_p10k, strict_p15k, strict_p20k, e_x_max, e_mean_max

# former plantagora and plantakolya
def align_and_analyze(is_cyclic, index, contigs_fpath, output_dirpath, ref_fpath,
                      reference_chromosomes, ns_by_chromosomes, old_contigs_fpath, bed_fpath, threads=1, contig_length_map=None):
    logger.info('  Running align_and_analyze with ' + str(threads) + ' threads')
    #logger.info(f"  {is_cyclic=}\n  {index=}\n  {contigs_fpath=}\n  {output_dirpath=}\n  {ref_fpath=}\n  {reference_chromosomes=}\n  {ns_by_chromosomes=}\n  {old_contigs_fpath=}\n  {bed_fpath=}\n  {threads=}")

    tmp_output_dirpath = create_minimap_output_dir(output_dirpath)
    assembly_label = qutils.label_from_fpath(contigs_fpath)
    corr_assembly_label = qutils.label_from_fpath_for_fname(contigs_fpath)
    out_basename = join(tmp_output_dirpath, corr_assembly_label)

    logger.info(f"  {tmp_output_dirpath=}\n  {out_basename=}")

    logger.info('  ' + qutils.index_to_str(index) + assembly_label)

    if not qconfig.space_efficient:
        log_out_fpath = join(output_dirpath, qconfig.contig_report_fname_pattern % corr_assembly_label + '.stdout')
        log_err_fpath = join(output_dirpath, qconfig.contig_report_fname_pattern % corr_assembly_label + '.stderr')
        icarus_out_fpath = join(output_dirpath, qconfig.icarus_report_fname_pattern % corr_assembly_label)
        misassembly_fpath = join(output_dirpath, qconfig.contig_report_fname_pattern % corr_assembly_label + '.mis_contigs.info')
        unaligned_info_fpath = join(output_dirpath, qconfig.contig_report_fname_pattern % corr_assembly_label + '.unaligned.info')
    else:
        log_out_fpath = '/dev/null'
        log_err_fpath = '/dev/null'
        icarus_out_fpath = '/dev/null'
        misassembly_fpath = '/dev/null'
        unaligned_info_fpath = '/dev/null'

    icarus_out_f = open(icarus_out_fpath, 'w')
    icarus_header_cols = ['S1', 'E1', 'S2', 'E2', 'Reference', 'Contig', 'IDY', 'Ambiguous', 'Best_group']
    icarus_out_f.write('\t'.join(icarus_header_cols) + '\n')
    misassembly_f = open(misassembly_fpath, 'w')

    if not qconfig.space_efficient:
        logger.info('  ' + qutils.index_to_str(index) + 'Logging to files ' + log_out_fpath +
                ' and ' + os.path.basename(log_err_fpath) + '...')
    else:
        logger.info('  ' + qutils.index_to_str(index) + 'Logging is disabled.')

    coords_fpath, coords_filtered_fpath, unaligned_fpath, used_snps_fpath = get_aux_out_fpaths(out_basename)
    logger.info("  Aligning contigs")
    status = align_contigs(coords_fpath, out_basename, ref_fpath, contigs_fpath, old_contigs_fpath, index, threads,
                           log_out_fpath, log_err_fpath)
    if status != AlignerStatus.OK:
        with open(log_err_fpath, 'a') as log_err_f:
            if status == AlignerStatus.ERROR:
                logger.error('  ' + qutils.index_to_str(index) +
                         'Failed aligning contigs ' + qutils.label_from_fpath(contigs_fpath) +
                         ' to the reference (non-zero exit code). ' +
                         ('Run with the --debug flag to see additional information.' if not qconfig.debug else ''))
            elif status == AlignerStatus.FAILED:
                log_err_f.write(qutils.index_to_str(index) + 'Alignment failed for ' + contigs_fpath + ':' + coords_fpath + 'doesn\'t exist.\n')
                logger.info('  ' + qutils.index_to_str(index) + 'Alignment failed for ' + '\'' + assembly_label + '\'.')
            elif status == AlignerStatus.NOT_ALIGNED:
                log_err_f.write(qutils.index_to_str(index) + 'Nothing aligned for ' + contigs_fpath + '\n')
                logger.info('  ' + qutils.index_to_str(index) + 'Nothing aligned for ' + '\'' + assembly_label + '\'.')
        return status, {}, [], [], []

    log_out_f = open(log_out_fpath, 'a')
    # Loading the alignment files
    log_out_f.write('Parsing coords...\n')
    aligns = {}
    with open(coords_fpath) as coords_file:
        logger.info("  Opening coords_fpath '" + str(coords_fpath) + "'")
        log_out_f.write("Opening coords_fpath '" + str(coords_fpath) + "'\n")
        for line in coords_file:
            mapping = Mapping.from_line(line)
            aligns.setdefault(mapping.contig, []).append(mapping)

    # Loading the reference sequences
    log_out_f.write('Loading reference...\n') # TODO: move up
    ref_features = {}

    # Loading the regions (if any)
    regions = {}
    total_reg_len = 0
    total_regions = 0
    # # TODO: gff
    # log_out_f.write('Loading regions...\n')
    # log_out_f.write('\tNo regions given, using whole reference.\n')
    for name, seq_len in reference_chromosomes.items():
        log_out_f.write('\tLoaded [%s]\n' % name)
        regions.setdefault(name, []).append([1, seq_len])
        total_regions += 1
        total_reg_len += seq_len
    log_out_f.write('\tTotal Regions: %d\n' % total_regions)
    log_out_f.write('\tTotal Region Length: %d\n' % total_reg_len)

    ca_output = CAOutput(stdout_f=log_out_f, misassembly_f=misassembly_f, coords_filtered_f=open(coords_filtered_fpath, 'w'),
                         icarus_out_f=icarus_out_f)

    log_out_f.write('Analyzing contigs...\n')
    result, ref_aligns, total_indels_info, aligned_lengths, misassembled_contigs, misassemblies_in_contigs, aligned_lengths_by_contigs =\
        analyze_contigs(ca_output, contigs_fpath, unaligned_fpath, unaligned_info_fpath, aligns, ref_features, reference_chromosomes, is_cyclic)

    ref_aligns_lengths = [align.len2_excluding_local_misassemblies for aligns in ref_aligns.values() for align in aligns]
    ref_aligns_lengths.sort(reverse=True)
    computed_aligns_lengths = aligned_lengths.copy()
    computed_aligns_lengths.sort(reverse=True)
    #logger.info("    Lengths of ref      aligns = " + str(ref_aligns_lengths))
    #logger.info("    Computed lengths of aligns = " + str(computed_aligns_lengths))
    logger.info("    Genome length = " + str(total_reg_len))
    logger.info("    Ref      NGA50 = " + str(N50.NG50_and_LG50(ref_aligns_lengths, total_reg_len, need_sort=True)[0]))
    logger.info("    Computed NGA50 = " + str(N50.NG50_and_LG50(computed_aligns_lengths, total_reg_len, need_sort=True)[0]))

    log_out_f.write('Analyzing coverage...\n')
    if qconfig.show_snps:
        log_out_f.write('Writing SNPs into ' + used_snps_fpath + '\n')
    total_aligned_bases, indels_info, ea_x_max, strict_ea_x_max, ea_mean_max, strict_ea_mean_max, p5k, p10k, p15k, p20k, strict_p5k, strict_p10k, strict_p15k, strict_p20k, e_x_max, e_mean_max =\
        analyze_coverage(ref_aligns, reference_chromosomes, ns_by_chromosomes, used_snps_fpath, contig_length_map)
    total_indels_info += indels_info
    cov_stats = {
        'SNPs': total_indels_info.mismatches,
        'indels_list': total_indels_info.indels_list,
        'total_aligned_bases': total_aligned_bases,
        'ea_x_max': ea_x_max,
        'strict_ea_x_max': strict_ea_x_max,
        'ea_mean_max': ea_mean_max,
        'strict_ea_mean_max': strict_ea_mean_max,
        'p5k': p5k,
        'p10k': p10k,
        'p15k': p15k,
        'p20k': p20k,
        'strict_p5k': strict_p5k,
        'strict_p10k': strict_p10k,
        'strict_p15k': strict_p15k,
        'strict_p20k': strict_p20k,
        'e_x_max': ea_x_max,
        'e_mean_max': e_mean_max,
    }
    result.update(cov_stats)
    result = print_results(contigs_fpath, log_out_f, used_snps_fpath, total_indels_info, result)

    if not qconfig.space_efficient:
        ## outputting misassembled contigs to separate file
        fasta = [(name, seq) for name, seq in fastaparser.read_fasta(contigs_fpath)
                 if name in misassembled_contigs.keys()]
        fastaparser.write_fasta(join(output_dirpath, qutils.name_from_fpath(contigs_fpath) + '.mis_contigs.fa'), fasta)

    if qconfig.is_combined_ref:
        alignment_tsv_fpath = join(output_dirpath, "alignments_" + corr_assembly_label + '.tsv')
        unique_contigs_fpath = join(output_dirpath, qconfig.unique_contigs_fname_pattern % corr_assembly_label)
        logger.debug('  ' + qutils.index_to_str(index) + 'Alignments: ' + qutils.relpath(alignment_tsv_fpath))
        used_contigs = set()
        with open(unique_contigs_fpath, 'w') as unique_contigs_f:
            with open(alignment_tsv_fpath, 'w') as alignment_tsv_f:
                for chr_name, aligns in ref_aligns.items():
                    alignment_tsv_f.write(chr_name)
                    contigs = set([align.contig for align in aligns])
                    for contig in contigs:
                        alignment_tsv_f.write('\t' + contig)

                    if qconfig.is_combined_ref:
                        ref_name = ref_labels_by_chromosomes[chr_name]
                        align_by_contigs = defaultdict(int)
                        for align in aligns:
                            align_by_contigs[align.contig] += align.len2
                        for contig, aligned_len in align_by_contigs.items():
                            if contig in used_contigs:
                                continue
                            used_contigs.add(contig)
                            len_cov_pattern = re.compile(r'_length_([\d\.]+)_cov_([\d\.]+)')
                            if len_cov_pattern.findall(contig):
                                contig_len = len_cov_pattern.findall(contig)[0][0]
                                contig_cov = len_cov_pattern.findall(contig)[0][1]
                                if aligned_len / float(contig_len) > 0.9:
                                    unique_contigs_f.write(ref_name + '\t' + str(aligned_len) + '\t' + contig_cov + '\n')
                    alignment_tsv_f.write('\n')

    close_handlers(ca_output)
    logger.info('  ' + qutils.index_to_str(index) + 'Analysis is finished.')
    logger.debug('')
    if not ref_aligns:
        return AlignerStatus.NOT_ALIGNED, result, aligned_lengths, misassemblies_in_contigs, aligned_lengths_by_contigs
    else:
        return AlignerStatus.OK, result, aligned_lengths, misassemblies_in_contigs, aligned_lengths_by_contigs


def do(reference, contigs_fpaths, is_cyclic, output_dir, old_contigs_fpaths, bed_fpath=None, contig_length_map=None):
    if not os.path.isdir(output_dir):
        os.mkdir(output_dir)

    logger.print_timestamp()
    logger.main_info('Running Contig analyzer...')
    success_compilation = compile_aligner(logger)
    if not success_compilation:
        logger.main_info('Failed aligning the contigs for all the assemblies. Only basic stats are going to be evaluated.')
        return dict(zip(contigs_fpaths, [AlignerStatus.FAILED] * len(contigs_fpaths))), None

    num_nf_errors = logger._num_nf_errors
    create_minimap_output_dir(output_dir)
    n_jobs = min(len(contigs_fpaths), qconfig.max_threads)
    threads = max(1, qconfig.max_threads // n_jobs)

    genome_size, reference_chromosomes, ns_by_chromosomes = get_genome_stats(reference, skip_ns=True)
    threads = qconfig.max_threads if qconfig.memory_efficient else threads
    args = [(is_cyclic, i, contigs_fpath, output_dir, reference, reference_chromosomes, ns_by_chromosomes,
            old_contigs_fpath, bed_fpath, threads, contig_length_map)
            for i, (contigs_fpath, old_contigs_fpath) in enumerate(zip(contigs_fpaths, old_contigs_fpaths))]
    statuses, results, aligned_lengths, misassemblies_in_contigs, aligned_lengths_by_contigs = run_parallel(align_and_analyze, args, n_jobs)
    reports = []

    aligner_statuses = dict(zip(contigs_fpaths, statuses))
    aligned_lengths_per_fpath = dict(zip(contigs_fpaths, aligned_lengths))
    aligned_lengths = [l for li in aligned_lengths for l in li]
    misc.contigs_aligned_lengths = dict(zip(contigs_fpaths, aligned_lengths_by_contigs))

    if AlignerStatus.OK in aligner_statuses.values():
        if qconfig.is_combined_ref:
            save_combined_ref_stats(results, contigs_fpaths, ref_labels_by_chromosomes, output_dir, logger)

    for index, fname in enumerate(contigs_fpaths):
        report = reporting.get(fname)
        if statuses[index] == AlignerStatus.OK:
            reports.append(save_result(results[index], report, fname, reference, genome_size, aligned_lengths))
        elif statuses[index] == AlignerStatus.NOT_ALIGNED:
            save_result_for_unaligned(results[index], report)

    if AlignerStatus.OK in aligner_statuses.values():
        all_breakpoints = {}
        for result in results:
            if "local_contig_breakpoints" in result:
                for key, value in result["local_contig_breakpoints"].items():
                    all_breakpoints.setdefault(key, []).extend(value)
            if "extensive_contig_breakpoints" in result:
                for key, value in result["extensive_contig_breakpoints"].items():
                    all_breakpoints.setdefault(key, []).extend(value)

        import json
        with open(join(output_dir, 'contig_breakpoints.json'), 'w') as out_file:
            out_file.write(json.dumps(all_breakpoints, sort_keys = True))

        reporting.save_misassemblies(output_dir)
        reporting.save_unaligned(output_dir)
        from . import plotter
        if qconfig.draw_plots:
            plotter.draw_misassemblies_plot(reports, join(output_dir, 'misassemblies_plot'), 'Misassemblies')
        if qconfig.draw_plots or qconfig.html_report:
            misassemblies_in_contigs = dict((contigs_fpaths[i], misassemblies_in_contigs[i]) for i in range(len(contigs_fpaths)))
            plotter.frc_plot(dirname(output_dir), reference, contigs_fpaths, misc.contigs_aligned_lengths, misassemblies_in_contigs,
                             join(output_dir, 'misassemblies_frcurve_plot'), 'misassemblies')

    oks = list(aligner_statuses.values()).count(AlignerStatus.OK)
    not_aligned = list(aligner_statuses.values()).count(AlignerStatus.NOT_ALIGNED)
    failed = list(aligner_statuses.values()).count(AlignerStatus.FAILED)
    errors = list(aligner_statuses.values()).count(AlignerStatus.ERROR)
    problems = not_aligned + failed + errors
    all = len(aligner_statuses)

    logger._num_nf_errors = num_nf_errors + errors

    if oks == all:
        logger.main_info('Done.')
    if oks < all and problems < all:
        logger.main_info('Done for ' + str(all - problems) + ' out of ' + str(all) + '. For the rest, only basic stats are going to be evaluated.')
    if problems == all:
        logger.main_info('Failed aligning the contigs for all the assemblies. Only basic stats are going to be evaluated.')

    return aligner_statuses, aligned_lengths_per_fpath
