from Bio import SeqIO, Seq, AlignIO
from Bio.Align import AlignInfo
import multiprocessing
import pathlib
import argparse
import tempfile
import io
import subprocess
import pandas
import numpy as np
import csv
import time

class alignment: 
    def __init__(self, alignment_path): 
        self.alignment = AlignIO.read(alignment_path, 'fasta')
        self.seq_position_data = None
        self.sequence_regions = dict()
        self.consensus = None
    def __repr__(self): 
        return self.alignment
    def get_consensus(self, threshold=0.9): 
        """
        
        """
        consensus=[]
        for base_position in self.seq_position_data.iterrows(): 
            nucleotide_counts = base_position[1].value_counts(normalize=True)
            if nucleotide_counts[0] >= threshold: 
                consensus.append(nucleotide_counts.index[0])
            else: 
                consensus.append("n")
        self.consensus = "".join(consensus).replace("-","")
    def _get_sequence_regions(self): 
        """
        Function to determine the start and end of sequences in an alignment
        alignment - Bio.Align.MultipleSeqAlignment object
        sequence region - list of tuples(start_index, end_index)
        0-based indices, half open
        """
        for sequence in self.alignment: 
            ungap_sequence = sequence.seq.ungap()
            start_base = ungap_sequence[0]
            end_base = ungap_sequence[-1]
            start_index = sequence.seq.find(start_base)
            end_index = sequence.seq.rfind(end_base)
            self.sequence_regions[sequence.id] = (start_index, end_index+1)
        return self.sequence_regions
    def _get_sequence_position_data(self):
        """
        Function to store information in a dataframe
        """
        dict_series = dict()
        for sequence in self.alignment: 
            #Create the series containing all of the data
            series = pandas.Series(list(sequence.seq), index=range(len(sequence.seq)), name=sequence.id)
            #Using sequence_regions, convert all non-sequence gap characters to NaN
            sequence_region = self.sequence_regions[sequence.id]
            #Start of the alignment to first base of sequence
            #Base past end of sequence to end of alignment
            series.iloc[0:sequence_region[0]] = np.NaN
            series.iloc[sequence_region[1]:len(sequence.seq)] = np.NaN
            dict_series[sequence.id] = series
        self.seq_position_data = pandas.DataFrame(dict_series)
        return self.seq_position_data
    def get_accessions(self): 
        list_id = []
        for seq in self.alignment: 
            list_id.append(seq.id.split('.')[0])
        return list_id

class probe:
    def __init__(self, root_pos, seq):
        self.seq = seq
        self.root_pos = root_pos
        self.len = len(self.seq)
        self.id =  f"{str(self.root_pos)}-{str(self.len)}"
        self.sensitivity = 0
        self.specificity = 0
        self.score = 0
    def calculate_sensitivity(self, blast_results, target_accessions):
        """
        Function calculates sensitivity of the oligo (binding to target sequences). 
        Binding is defined as 100% query coverage and 100% percent identity of the oligo to the target sequence.
        Sensitivity is calculated by the following formula: 
            TP / (TP + FN)
            where: 
            TP = succesfully amplified accessions
            FN = target accessions that were not amplified
            TP + FN = total number of target accessions
        """
        #Take only accessions where there was a perfect match --> full query coverage, 100% identity
        perfect_match_results = blast_results.loc[(blast_results['qlen']==blast_results['length'])&(blast_results['pident']==100.0)]
        #Retrieve only the accessions list
        amplified_accessions = set(perfect_match_results.loc[:,'sacc'])
        target_match = 0
        #Count number of target accessions in the amplified accession list 
        for accession in amplified_accessions: 
                if accession in target_accessions: 
                    target_match = target_match + 1
        #Calculate sensitivity and return
        self.sensitivity = target_match/len(target_accessions)
    def calculate_specificity(self, blast_results, target_accessions, blastdb_len): 
        """
        Function calculates specificity of the oligo (binding to non-target sequences). 
        Binding is defined as simply appearing in the BLAST results --> this will be a overestimation of the specificity,
        making it the 'worst case' scenario. 
        Specificity is calculated by the following formula: 
            TN / (TN + FP)
            where:
            TN = non-target accessions that were not amplified
            TN = (total non-target) - (amplified non-target)
            FP = amplified non-target
            resulting in the following formula: 
            ((Total non-target) - (amplified non-target)) / total non-target
        """
        blast_match_accessions = set(blast_results.loc[:,'sacc'])
        #Remove every target_accession from the blast_match_accessions list
        for accession in target_accessions:
            if accession in blast_match_accessions:  
                blast_match_accessions.remove(accession)
        #Calculate specificity
        #Total non-target = all_blast_sequences - target_accessions
        self.specificity = (blastdb_len - len(blast_match_accessions))/(blastdb_len - len(target_accessions))
    def calculate_score(self): 
        self.score = self.sensitivity + self.specificity

class probeGenerator: 
    def __init__(
        self, 
        template, 
        start, 
        end, 
        min_length, 
        max_length, 
        ):
        #Note that the coordinates are 1-based, so the template start has to be -1
        self.template = template[start:end]
        self.start = start
        self.end = end
        self.min_length = min_length
        self.max_length = max_length
        self.probes = []
    def get_probes(self): 
        """
        Function finds all viable probes. 
        Probe criteria: 
        1) 5' end of the probe is not a G
        2) More C's than G's
        3) No more than 4 nucleotides in a row
        4) 30 - 80% CG content
        """
        def check_probe(seq): 
            def count_c_g(seq): 
                return seq.count('c') + seq.count('g') + seq.count('C') + seq.count('G')
            def chk_run(seq): 
                """
                Ensure that there are no runs of four or more identical nucleotides in the probe
                """
                current = seq[0]
                identical_len = 1
                detected = False
                #Go through each nucleotide in the primer
                for nucl in range(1, len(seq)): 
                    if seq[nucl] == current: 
                        identical_len = identical_len + 1
                        if identical_len > 3: 
                            detected = True
                            break
                    else: 
                        current = seq[nucl]
                        identical_len = 1
                return detected
            def chk_last_5(seq): 
                return count_c_g(seq[-5:]) > 2
            #Check the three conditions
            percent_c_g = count_c_g(seq)/len(seq)
            run_flag = chk_run(seq)
            last5_flag = chk_last_5(seq)
            if(
                0.3 <= percent_c_g <= 0.8 
                and run_flag is False
                and last5_flag is False
                and seq[0] != 'G'
                and seq[0] != 'g'
                and not('n' in seq)
                and not('N' in seq)
            ):
                return True
            else: 
                return False
        
        #For each root position in the target sequence
        for i in range(len(self.template) - self.min_length + 1):
            #Iterate over all probe lengths
            for probe_len in range(self.min_length, self.max_length + 1): 
                probe_seq = self.template[i:i+probe_len]
                if check_probe(probe_seq) is True: 
                    #Note that the coordinates are converted back to 1-based
                    self.probes.append(probe(self.start+i+1, probe_seq))
    def output(self, path): 
        probe_data = []
        for probe in self.probes: 
            probe_data.append(
                (
                    probe.root_pos,
                    probe.len, 
                    probe.seq, 
                    probe.sensitivity, 
                    probe.specificity,
                    probe.score,
                )
            )
        output_path = path.with_name('probe_candidates.csv')
        csv_file = open(output_path, 'w', newline='')
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(
            (
                'probe_root',
                'probe_len',
                'probe_seq',
                'sens',
                'spec',
                'score',
            )
        )
        csv_writer.writerows(probe_data)
        csv_file.close()

class nemaBlast: 
    def __init__(self, blastdb, blastdb_len):
        self.blastdb = blastdb
        self.blastdb_len = blastdb_len
        self.data = None
    def blast_all(self, probes): 
        #Generate the probe temporary file
        fasta = tempfile.NamedTemporaryFile(delete=True)
        print('Probes in the temp file: ')
        for probe in probes:
            fasta.write(f">{str(probe.id)}\n{str(probe.seq)}\n".encode())
            print(probe.id)
            print(probe.seq)
        fasta.seek(0)
        cpu_count = multiprocessing.cpu_count() - 2
        print(f"NUMBER OF THREADS: {str(cpu_count)}")
        #Run the BLAST job
        args = [
            "blastn",
            "-task",
            "blastn-short",
            "-db",
            self.blastdb,
            "-num_alignments",
            str(self.blastdb_len),
            "-outfmt",
            "10 qacc sacc ssciname pident qlen length mismatch gapopen qstart qend sstart send evalue bitscore",
            "-query",
            fasta.name,
            "-num_threads",
            str(cpu_count),
            "-mt_mode",
            str(1)
        ]
        result = subprocess.run(args, capture_output=True)
        decoded = result.stdout.decode('utf-8')
        output = io.StringIO(decoded)
        #Output formatting into dataframe
        headers=[
            'qacc',
            'sacc',
            'ssciname',
            'pident',
            'qlen',
            'length',
            'mismatch', 
            'gapopen', 
            'qstart', 
            'qend', 
            'sstart', 
            'send', 
            'evalue', 
            'bitscore',
        ]
        self.data = pandas.read_csv(output, sep=',', header=None, names=headers)
        print(self.data.loc[self.data['qacc']=='1663-17'])
        fasta.close()
        #Split the data
        list_probe_ids = set(self.data['qacc'])
        print(f"qacc set:\n{list_probe_ids}")
        blast_results = dict()
        for probe_id in list_probe_ids: 
            blast_results[str(probe_id)]=self.data.loc[self.data['qacc']==probe_id]
        return blast_results
    def output(self, blast_results, path): 
        #Make path to store all of the blast results
        blast_folder_path = path.joinpath('probe_blast')
        blast_folder_path.mkdir(exist_ok=True)
        #Go through blast result dictionary and output all of the data
        for blast_result_key in blast_results: 
            blast_output_path = blast_folder_path.joinpath(f"{blast_result_key}_probe.csv")
            blast_output_file = open(blast_output_path, 'w')
            blast_results[blast_result_key].to_csv(blast_output_file)
            blast_output_file.close()

def print_runtime(action) -> None:
    """ Print the time and some defined action. """
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}] {action}')

def parse_args(): 
    parser = argparse.ArgumentParser(description='probesearch.py - identify viable probes in an alignment for given target sequences')
    parser.add_argument(
        'target_alignment_path', 
        action='store', 
        type=pathlib.Path,
        help = 'Path to target alignment file, fasta format'
    )
    parser.add_argument(
        '--output', 
        metavar='output_directory',
        dest='output_path',
        default=None,
        action='store',
        type=pathlib.Path, 
        help='Output path',
    )
    parser.add_argument(
        '--target_start',
        metavar='target_start', 
        dest='target_start',
        default=1,
        action='store',
        type=int,
        help='Start coordinate of target region, 1-based coordinates'
    )
    parser.add_argument(
        '--target_end',
        metavar='target_end', 
        dest='target_end', 
        default=None,
        action='store',
        type=int,
        help='End coordinate of target region, 1-based coordinates'
    )
    parser.add_argument(
        '--min_primer_len',
        action='store',
        type=int, 
        default=17,
        dest='min_primer_len',
        help='Minimum primer length (default=17)'
    )
    parser.add_argument(
        '--max_primer_len',
        action='store',
        type=int, 
        default=22,
        dest='max_primer_len',
        help='Maximum primer length (default=22)'
    )
    parser.add_argument(
        '--no_sens_spec_check',
        action='store_true',
        dest='sens_spec_flag',
        help='Flag to not check the putative probes for their specificity and sensitivity'
    )
    #Arguments for specificity checking
    parser.add_argument(
        '--blastdb',
        action='store',
        type=str,
        dest='blastdb',
        default='',
        help='Name of blastdb'
    )
    parser.add_argument(
        '--blastdb_len',
        action='store',
        type=int,
        dest='blastdb_len',
        help='Length of blastdb'
    )
    args = parser.parse_args()
    if not args.output_path:
        args.output_path = args.target_alignment_path.parent
    #Note that coordinates are converted to 0-based half-open coordinates
    return (
        args.target_alignment_path, 
        args.output_path, 
        args.target_start-1, 
        args.target_end, 
        args.min_primer_len, 
        args.max_primer_len, 
        args.sens_spec_flag, 
        args.blastdb, 
        args.blastdb_len, 
        args.target_accessions
    )

def main():
    #Arguments
    target_alignment_path, output_path, target_start, target_end, min_primer_len, max_primer_len, check_flag, blastdb, blastdb_len = parse_args()
    #Process the alignment
    target_alignment = alignment(target_alignment_path)
    target_consensus = target_alignment.get_consensus()
    target_accessions = target_alignment.get_accessions()
    #Generate Probes
    print_runtime("Start")
    pb_gen = probeGenerator(target_consensus, target_start, target_end, min_primer_len, max_primer_len)
    print("Generating probes...")
    pb_gen.get_probes()
    print("Probes finished!")
    #Do the specificity check
    if check_flag is False: 
        #Read target accessions
        #target_accessions = get_target_accessions(target_accession_path)
        #Generate BLAST results
        pb_blast = nemaBlast(blastdb, blastdb_len)
        blast_results = pb_blast.blast_all_proper(pb_gen.probes)
        #Output BLAST results
        pb_blast.output(blast_results, output_path)
        for probe in pb_gen.probes: 
            probe.calculate_sensitivity(blast_results[probe.id], target_accessions)
            probe.calculate_specificity(blast_results[probe.id], target_accessions, blastdb_len)
            probe.calculate_score()
        #Output probe list
        pb_gen.output(output_path)
    else: 
        pb_gen.output(output_path)
    print_runtime("End")
if __name__ == '__main__': 
    main()