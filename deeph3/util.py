import torch
import torch.nn as nn
import torch.nn.functional as F
import math
import re
import argparse
from os.path import splitext, basename, isfile
from Bio import SeqIO
from Bio.PDB import PDBParser
from Bio.SeqUtils import seq1
from deeph3 import H3ResNet


class RawTextArgumentDefaultsHelpFormatter(
        argparse.ArgumentDefaultsHelpFormatter,
        argparse.RawTextHelpFormatter):
    """CLI help formatter that includes the default value in the help dialog
    and formats as raw text i.e. can use escape characters."""
    pass


_aa_dict = {'A': '0', 'C': '1', 'D': '2', 'E': '3', 'F': '4', 'G': '5', 'H': '6', 'I': '7', 'K': '8', 'L': '9', 'M': '10', 'N': '11', 'P': '12', 'Q': '13', 'R': '14', 'S': '15', 'T': '16', 'V': '17', 'W': '18', 'Y': '19'}


def letter_to_num(string, dict_):
    """Function taken from ProteinNet (https://github.com/aqlaboratory/proteinnet/blob/master/code/text_parser.py).
    Convert string of letters to list of ints"""
    patt = re.compile('[' + ''.join(dict_.keys()) + ']')
    num_string = patt.sub(lambda m: dict_[m.group(0)] + ' ', string)
    num = [int(i) for i in num_string.split()]
    return num


def time_diff(start_time, end_time):
    """Returns the difference in time in HH:MM:SS format"""
    secs = int((end_time - start_time) % 60)
    mins = int(((end_time - start_time) // 60) % 60)
    hrs = int(((end_time - start_time) // (60 * 60)) % 60)
    return '{}:{:02}:{:02} (hrs:min:secs)'.format(hrs, mins, secs)


def one_hot_seq(seq):
    """Gets a one-hot encoded version of a protein sequence"""
    return F.one_hot(torch.LongTensor(letter_to_num(seq, _aa_dict)))


def bin_matrix(in_tensor, are_logits=True, method='max'):
    """
    Bins a 3d tensor of shape (logits, N, N). This assumes that the channels
    are logits to generate probabilities from.

    :param in_tensor: The tensor to bin.
    :type in_tensor: torch.Tensor
    :param are_logits:
        Whether or not the tensor consists of logits to turn into
        probabilities. If not, they are assumed to be probabilities.
    :param method:
        The binning method. Can either be 'max' or 'average'. 'max' will
        assign an element to the bin with the highest probability and 'average'
        will assign an element to the weighted average of the bins
    :return:
    """
    if are_logits:
        probs = generate_probabilities(in_tensor)
    else:
        probs = in_tensor
    if method == 'max':
        # Predict the bins with the highest probability
        return probs.max(len(probs.shape)-1)[1]
    elif method == 'avg':
        # Predict the bin that is closest to the average of the probability dist
        # predicted_bins[i][j] = round(sum(bin_index * P(bin_index at i,j)))
        bin_indices = torch.arange(probs.shape[-1]).float()
        predicted_bins = torch.round(torch.sum(probs.mul(bin_indices),
                                               dim=len(probs.shape)-1))
        return predicted_bins
    else:
        raise ValueError('method must be in {\'avg\',\'max\'}')


def load_full_seq(fasta_file):
    """Concatenates the sequences of all the chains in a fasta file"""
    with open(fasta_file, 'r') as f:
        return ''.join([seq.rstrip() for seq in f.readlines() if seq[0] != '>'])


def get_logits_from_model(model, fasta_file, chain_delimiter=True):
    """Gets the probability distribution output of a H3ResNet model"""
    seq = one_hot_seq(load_full_seq(fasta_file)).float()
    if chain_delimiter:
        # Add chain delimiter
        seq = F.pad(seq, (0, 1, 0, 0))
        h_len = 0
        for chain in SeqIO.parse(fasta_file, 'fasta'):
            if ':H' in chain.id:
                h_len = len(chain.seq)
        if h_len == 0:
            raise ValueError('No heavy chain detected. Cannot add chain delimiter')
        seq[h_len-1, seq.shape[1]-1] = 1

    seq = seq.unsqueeze(0).transpose(1, 2)
    with torch.no_grad():
        return model(seq)[0]


def generate_probabilities(logits):
    """Transforms a 4d tensor of logits of shape (outmats, logits, N, N) to probabilities"""
    if len(logits.shape) != 4:
        raise ValueError('Expected a shape with four dimensions (outmats, channels, L, L), got {}'.format(logits.shape))

    # Transform from [outmats, channels, L_i, L_j] to [outmats, L_i, L_j, channels]
    logits = logits.transpose(1, 2)
    logits = logits.transpose(2, 3)

    # Get the probabilities of each bin at each position and predict the bins
    return nn.Softmax(dim=3)(logits)


def get_probs_from_model(model, fasta_file, **kwargs):
    """Gets the probability distribution output of a H3ResNet model"""
    logits = get_logits_from_model(model, fasta_file, **kwargs)
    return generate_probabilities(logits)


def get_dist_bins(num_bins):
    first_bin = 4
    bins = [(first_bin + 0.5 * i, first_bin + 0.5 + 0.5 * i) for i in range(num_bins - 2)]
    bins.append((bins[-1][1], float('Inf')))
    bins.insert(0, (0, first_bin))
    return bins


def get_omega_bins(num_bins):
    first_bin = -180
    bin_width = 2 * 180 / num_bins
    bins = [(first_bin + bin_width * i, first_bin + bin_width * (i + 1)) for i in range(num_bins)]
    return bins


def get_theta_bins(num_bins):
    first_bin = -180
    bin_width = 2 * 180 / num_bins
    bins = [(first_bin + bin_width * i, first_bin + bin_width * (i + 1)) for i in range(num_bins)]
    return bins


def get_phi_bins(num_bins):
    first_bin = 0
    bin_width = 180 / num_bins
    bins = [(first_bin + bin_width * i, first_bin + bin_width * (i + 1)) for i in range(num_bins)]
    return bins


def get_bin_values(bins):
    bin_values = [t[0] for t in bins]
    bin_width = (bin_values[2] - bin_values[1]) / 2
    bin_values = [v + bin_width for v in bin_values]
    bin_values[0] = bin_values[1] - 2 * bin_width
    return bin_values


def bin_dist_angle_matrix(dist_angle_mat, num_bins=26):
    dist_bins = get_dist_bins(num_bins)
    omega_bins = get_omega_bins(num_bins)
    theta_bins = get_theta_bins(num_bins)
    phi_bins = get_phi_bins(num_bins)

    binned_matrix = torch.zeros(dist_angle_mat.shape, dtype=torch.long)
    for i, (lower_bound, upper_bound) in enumerate(dist_bins):
        bin_mask = (dist_angle_mat[0] >= lower_bound).__and__(dist_angle_mat[0] < upper_bound)
        binned_matrix[0][bin_mask] = i
    for i, (lower_bound, upper_bound) in enumerate(omega_bins):
        bin_mask = (dist_angle_mat[1] >= lower_bound).__and__(dist_angle_mat[1] < upper_bound)
        binned_matrix[1][bin_mask] = i
    for i, (lower_bound, upper_bound) in enumerate(theta_bins):
        bin_mask = (dist_angle_mat[2] >= lower_bound).__and__(dist_angle_mat[2] < upper_bound)
        binned_matrix[2][bin_mask] = i
    for i, (lower_bound, upper_bound) in enumerate(phi_bins):
        bin_mask = (dist_angle_mat[3] >= lower_bound).__and__(dist_angle_mat[3] < upper_bound)
        binned_matrix[3][bin_mask] = i

    return binned_matrix


def generate_dist_matrix(coords, mask=None, mask_fill_value=-1):
    """Generates a matrix of pairwise distances for a given list of coordinates.

    :param tertiary:
        An nx3 tensor of coordinates.
    :type tertiary: torch.Tensor
    :param mask: A tensor of shape (n,) with 1's on valid elements and 0 on
                 invalid elements in the sequence.
    :type mask: torch.Tensor
    :param mask_fill_value: The value to replace invalid elements with.
    :type mask_fill_value: int
    :return: A distance matrix of distances between alpha-carbons.
    :rtype: torch.Tensor
    """
    coords = coords.unsqueeze(0)
    dist_mat_shape = (coords.shape[1], coords.shape[1], coords.shape[2])
    row_expand = coords.transpose(0, 1).expand(dist_mat_shape)
    col_expand = coords.expand(dist_mat_shape)
    dist_mat = (row_expand - col_expand).norm(dim=2)

    if mask is not None:
        n = len(mask)
        not_mask = torch.ones(n).type(dtype=mask.dtype) - mask  # Set 1's to 0's and vice versa

        # Expand not_mask to an nxn Tensor such that row i is filled with
        # not_mask[i]'s value, then add the original not_mask vector to each row.
        # Example:
        # not_mask = [0, 0, 1, 1, 0]
        #             |0, 0, 0, 0, 0|   |0, 0, 1, 1, 0|   |0, 0, 1, 1, 0|
        #             |0, 0, 0, 0, 0|   |0, 0, 1, 1, 0|   |0, 0, 1, 1, 0|
        # operation = |1, 1, 1, 1, 1| + |0, 0, 1, 1, 0| = |1, 1, 2, 2, 1|
        #             |1, 1, 1, 1, 1|   |0, 0, 1, 1, 0|   |1, 1, 2, 2, 1|
        #             |0, 0, 0, 0, 0|   |0, 0, 1, 1, 0|   |0, 0, 1, 1, 0|
        not_mask = not_mask.unsqueeze(0).transpose(0, 1).expand(n, n).add(not_mask)
        dist_mat[not_mask > 0] = mask_fill_value

    return dist_mat


def generate_cb_cb_dihedral(ca_coords, cb_coords, mask=None, mask_fill_value=-1):    
    mat_shape = (ca_coords.shape[0], ca_coords.shape[0], ca_coords.shape[1])

    b1 = (cb_coords - ca_coords).expand(mat_shape)
    b2 = cb_coords.expand(mat_shape)
    b2 = b2.transpose(0, 1) - b2
    b3 = -1 * b1.transpose(0, 1)

    n1 = torch.cross(b1, b2)
    n1 /= n1.norm(dim=2, keepdim=True)
    n2 = torch.cross(b2, b3)
    n2 /= n2.norm(dim=2, keepdim=True)
    m1 = torch.cross(b2 / b2.norm(dim=2, keepdim=True), n1)

    dihedral_mat = torch.atan2((m1 * n2).sum(-1), (n1 * n2).sum(-1))
    dihedral_mat *= 180 / math.pi

    mask = mask.expand((len(mask), len(mask)))
    mask = mask & mask.transpose(0, 1)
    dihedral_mat[mask == 0] = mask_fill_value

    return dihedral_mat


def generate_ca_cb_dihedral(ca_coords, cb_coords, n_coords, mask=None, mask_fill_value=-1):    
    mat_shape = (ca_coords.shape[0], ca_coords.shape[0], ca_coords.shape[1])

    b1 = (ca_coords - n_coords).expand(mat_shape)
    b2 = (cb_coords - ca_coords).expand(mat_shape)
    b3 = cb_coords.expand(mat_shape)
    b3 = b3.transpose(0, 1) - b3

    n1 = torch.cross(b1, b2)
    n1 /= n1.norm(dim=2, keepdim=True)
    n2 = torch.cross(b2, b3)
    n2 /= n2.norm(dim=2, keepdim=True)
    m1 = torch.cross(b2 / b2.norm(dim=2, keepdim=True), n1)

    dihedral_mat = torch.atan2((m1 * n2).sum(-1), (n1 * n2).sum(-1)).transpose(0, 1)
    dihedral_mat *= 180 / math.pi

    mask = mask.expand((len(mask), len(mask)))
    mask = mask & mask.transpose(0, 1)
    dihedral_mat[mask == 0] = mask_fill_value

    # for i in range(10):
    #     print(i+1, dihedral_mat[0,i], dihedral_mat[i,0])

    return dihedral_mat


def generate_ca_cb_cb_planar(ca_coords, cb_coords, mask=None, mask_fill_value=-1):
    mat_shape = (ca_coords.shape[0], ca_coords.shape[0], ca_coords.shape[1])

    v1 = (ca_coords - cb_coords).expand(mat_shape)
    v2 = cb_coords.expand(mat_shape)
    v2 = v2.transpose(0, 1) - v2

    planar_mat = (v1 * v2).sum(-1) / (v1.norm(dim=2) * v2.norm(dim=2))
    planar_mat = torch.acos(planar_mat).transpose(0, 1)
    planar_mat *= 180 / math.pi

    mask = mask.expand((len(mask), len(mask)))
    mask = mask & mask.transpose(0, 1)
    planar_mat[mask == 0] = mask_fill_value

    # for i in range(10):
    #     print(i+1, planar_mat[0,i], planar_mat[i,0])
    
    return planar_mat


def protein_dist_angle_matrix(pdb_file, mask=None):
    p = PDBParser()
    file_name = splitext(basename(pdb_file))[0]
    structure = p.get_structure(file_name, pdb_file)
    residues = [r for r in structure.get_residues()]

    def get_cb_or_ca_coord(residue):
        if 'CB' in residue:
            return residue['CB'].get_coord()
        elif 'CA' in residue:
            return residue['CA'].get_coord()
        else:
            return [0, 0, 0]

    def get_atom_coord(residue, atom_type):
        if atom_type in residue:
            return residue[atom_type].get_coord()
        else:
            return [0, 0, 0]

    cb_ca_coords = torch.tensor([get_cb_or_ca_coord(r) for r in residues])
    ca_coords = torch.tensor([get_atom_coord(r, 'CA') for r in residues])
    cb_coords = torch.tensor([get_atom_coord(r, 'CB') for r in residues])
    n_coords = torch.tensor([get_atom_coord(r, 'N') for r in residues])

    cb_mask = torch.ByteTensor([1 if sum(_) != 0 else 0 for _ in cb_coords])
    if mask is None:
        mask = torch.ByteTensor([1] * len(cb_coords))

    output_matrix = torch.stack([
        generate_dist_matrix(cb_ca_coords, mask=mask),
        generate_cb_cb_dihedral(ca_coords, cb_coords, mask=(mask & cb_mask)),
        generate_ca_cb_dihedral(ca_coords, cb_coords, n_coords, mask=(mask & cb_mask)),
        generate_ca_cb_cb_planar(ca_coords, cb_coords, mask=(mask & cb_mask))
    ])

    return output_matrix


def binned_dist_mat_to_values(dist_mat, num_bins=26):
    if len(dist_mat.shape) == 2:
        dist_bin_values = get_bin_values(get_dist_bins(num_bins))
        dist_value_mat = torch.zeros(dist_mat.shape[0], dist_mat.shape[1])
        for i in range(dist_value_mat.shape[0]):
            for j in range(dist_value_mat.shape[1]):
                dist_value_mat[i, j] = dist_bin_values[dist_mat[i, j]]
        return dist_value_mat


def binned_mat_to_values(binned_mat, num_bins=26):
    dist_bins = get_dist_bins(num_bins)
    omega_bins = get_omega_bins(num_bins)
    theta_bins = get_theta_bins(num_bins)
    phi_bins = get_phi_bins(num_bins)

    value_mat = torch.zeros(binned_mat.shape)
    if len(binned_mat.shape) == 3:
        for mat_i, bins in enumerate([dist_bins, omega_bins, theta_bins, phi_bins]):
            bin_values = get_bin_values(bins)
            for i in range(binned_mat.shape[1]):
                for j in range(binned_mat.shape[2]):
                    value_mat[mat_i, i, j] = bin_values[binned_mat[mat_i, i, j].item()]
    
    return value_mat


def max_shape(data):
    """Gets the maximum length along all dimensions in a list of Tensors"""
    shapes = torch.Tensor([_.shape for _ in data])
    return torch.max(shapes.transpose(0, 1), dim=1)[0].int()


def pad_data_to_same_shape(tensor_list, pad_value=0):
    target_shape = max_shape(tensor_list)

    padded_dataset_shape = [len(tensor_list)] + list(target_shape)
    padded_dataset = torch.Tensor(*padded_dataset_shape)
    for i, data in enumerate(tensor_list):
        # Get how much padding is needed per dimension
        padding = reversed(target_shape - torch.Tensor(list(data.shape)).int())

        # Add 0 every other index to indicate only right padding
        padding = F.pad(padding.unsqueeze(0).t(), (1, 0, 0, 0)).view(-1, 1)
        padding = padding.view(1, -1)[0].tolist()

        padded_data = F.pad(data, padding, value=pad_value)
        padded_dataset[i] = padded_data

    return padded_dataset


def fill_diagonally_(matrix, diagonal_index, fill_value=0, fill_method='below'):
    """Destructively fills an nxm tensor somehow with respect to a diagonal.
    :param matrix:
    :type matrix: torch.Tensor
    :param diagonal_index:
    :param fill_value:
    :type fill_value: numeric
    :param fill_method:
    :type fill_method: str
    :return:
    """
    num_rows = matrix.shape[0]
    if fill_method == 'symmetric':
        mask = torch.ones(matrix.shape)
        fill_diagonally_(mask, diagonal_index - 1, fill_method='between',
                         fill_value=0)
        matrix[mask.byte()] = fill_value
        return

    for i in range(num_rows):
        if fill_method == 'below':
            left_bound = 0
            right_bound = min(num_rows, max(i - diagonal_index + 1, 0))
        elif fill_method == 'above':
            left_bound = min(num_rows, max(i - diagonal_index, 0))
            right_bound = num_rows
        elif fill_method == 'between':
            left_bound = min(num_rows, max(i - diagonal_index, 0))
            right_bound = min(num_rows, min(i + diagonal_index + 1, num_rows))
        else:
            msg = ('{} is an invalid fill_method. The fill_method must be in '
                   '\'below\', \'above\', \'symmetric\', \'between\'')
            raise ValueError(msg.format(fill_method))

        matrix[i, left_bound:right_bound] = fill_value


def pdb2fasta(pdb_file, num_chains=None):
    """Converts a PDB file to a fasta formatted string using its ATOM data"""
    pdb_id = basename(pdb_file).split('.')[0]
    parser = PDBParser()
    structure = parser.get_structure(pdb_id, pdb_file)

    real_num_chains = len([0 for _ in structure.get_chains()])
    if num_chains is not None and num_chains != real_num_chains:
        print('WARNING: Skipping {}. Expected {} chains, got {}'.format(
            pdb_file, num_chains, real_num_chains))
        return ''

    fasta = ''
    for chain in structure.get_chains():
        id_ = chain.id
        seq = seq1(''.join([residue.resname for residue in chain]))
        fasta += '>{}:{}\t{}\n'.format(pdb_id, id_, len(seq))
        max_line_length = 80
        for i in range(0, len(seq), max_line_length):
            fasta += f'{seq[i:i + max_line_length]}\n'
    return fasta


def get_fasta_basename(fasta_file):
    base = basename(fasta_file) # extract filename w/o path
    if splitext(base)[1]=='.fasta': base = splitext(base)[0]  # remove .fasta if present
    return base


def load_model(file_name, num_blocks1D=3, num_blocks2D=25):
    """Loads a model from a pickle file

    :param file_name:
        A pickle file containing a dictionary with the following keys:
            state_dict: The state dict of the H3ResNet model
            num_blocks1D: The number of one dimensional ResNet blocks
            num_blocks2D: The number of two dimensional ResNet blocks
            dilation (optional): The dilation cycle of the model
    :param num_blocks1D:
        If num_blocks1D is not in the pickle file, then this number is used for
        the amount of one dimensional residual blocks.
    :param num_blocks2D:
        If num_blocks2D is not in the pickle file, then this number is used for
        the amount of two dimensional residual blocks.
    """
    if not isfile(file_name):
        raise FileNotFoundError(f'No file at {file_name}')
    checkpoint_dict = torch.load(file_name, map_location='cpu')
    model_state = checkpoint_dict['model_state_dict']

    dilation_cycle = 0 if not 'dilation_cycle' in checkpoint_dict else checkpoint_dict[
        'dilation_cycle']

    in_layer = list(model_state.keys())[0]
    out_layer = list(model_state.keys())[-1]
    num_out_bins = model_state[out_layer].shape[0]
    in_planes = model_state[in_layer].shape[1]

    if 'num_blocks1D' in checkpoint_dict:
        num_blocks1D = checkpoint_dict['num_blocks1D']
    if 'num_blocks2D' in checkpoint_dict:
        num_blocks2D = checkpoint_dict['num_blocks2D']

    resnet = H3ResNet(in_planes=in_planes, num_out_bins=num_out_bins,
                      num_blocks1D=num_blocks1D, num_blocks2D=num_blocks2D,
                      dilation_cycle=dilation_cycle)
    model = nn.Sequential(resnet)
    model.load_state_dict(model_state)
    model.eval()

    return model

