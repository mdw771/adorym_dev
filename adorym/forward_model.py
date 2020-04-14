import numpy as np
from scipy.ndimage import rotate as sp_rotate

import gc
import time

import adorym.wrappers as w
from adorym.util import *
from adorym.propagate import multislice_propagate_batch, get_kernel

class ForwardModel(object):

    def __init__(self, loss_function_type='lsq', distribution_mode=None, device=None, common_vars_dict=None, raw_data_type='magnitude'):
        self.loss_function_type = loss_function_type
        self.argument_ls = []
        self.regularizer_dict = {}
        self.distribution_mode = distribution_mode
        self.device = device
        self.current_loss = 0
        self.common_vars = common_vars_dict
        self.raw_data_type = raw_data_type
        self.unknown_type = common_vars_dict['unknown_type']
        self.i_call = 0
        self.normalize_fft = common_vars_dict['normalize_fft']
        self.sign_convention = common_vars_dict['sign_convention']
        self.rotate_out_of_loop = common_vars_dict['rotate_out_of_loop']

    def add_regularizer(self, name, reg_dict):
        self.regularizer_dict[name] = reg_dict

    def add_l1_norm(self, alpha_d, alpha_b):
        d = {'alpha_d': alpha_d,
             'alpha_b': alpha_b}
        self.add_regularizer('l1_norm', d)

    def add_reweighted_l1_norm(self, alpha_d, alpha_b, weight_l1):
        d = {'alpha_d': alpha_d,
             'alpha_b': alpha_b,
             'weight_l1': weight_l1}
        self.add_regularizer('reweighted_l1', d)

    def add_tv(self, gamma):
        d = {'gamma': gamma}
        self.add_regularizer('tv', d)

    def update_l1_weight(self, weight_l1):
        self.regularizer_dict['reweighted_l1']['weight_l1'] = weight_l1

    def get_regularization_value(self, obj):
        reg = w.create_variable(0., device=self.device)
        for name in list(self.regularizer_dict):
            if name == 'l1_norm':
                reg = reg + l1_norm_term(obj,
                                    self.regularizer_dict[name]['alpha_d'],
                                    self.regularizer_dict[name]['alpha_b'],
                                    device=self.device)
            elif name == 'reweighted_l1_norm':
                reg = reg + reweighted_l1_norm_term(obj,
                                               self.regularizer_dict[name]['alpha_d'],
                                               self.regularizer_dict[name]['alpha_b'],
                                               self.regularizer_dict[name]['weight_l1'],
                                               device=self.device)
            elif name == 'tv':
                if self.unknown_type == 'delta_beta':
                    reg = reg + tv(obj,
                              self.regularizer_dict[name]['gamma'],
                              self.distribution_mode, device=self.device)
                elif self.unknown_type == 'real_imag':
                    slicer = [slice(None)] * (len(obj.shape) - 1)
                    reg = reg + tv(w.arctan2(obj[slicer + [1]], obj[slicer + [0]]), None,
                              self.regularizer_dict[name]['gamma'],
                              self.distribution_mode, device=self.device)
        return reg

    def get_argument_index(self, arg):
        for i, a in enumerate(self.argument_ls):
            if a == arg:
                return i
        raise ValueError('{} is not in the argument list.'.format(arg))


class PtychographyModel(ForwardModel):

    def __init__(self, loss_function_type='lsq', distribution_mode=None, device=None, common_vars_dict=None, raw_data_type='magnitude'):
        super(PtychographyModel, self).__init__(loss_function_type, distribution_mode, device, common_vars_dict, raw_data_type)
        # ==========================================================================================
        # argument_ls must be in the same order as arguments in get_loss_function's function call!
        # ==========================================================================================
        self.argument_ls = ['obj', 'probe_real', 'probe_imag', 'probe_defocus_mm',
                            'probe_pos_offset', 'this_i_theta', 'this_pos_batch', 'prj',
                            'probe_pos_correction', 'this_ind_batch']

    def predict(self, obj, probe_real, probe_imag, probe_defocus_mm,
                probe_pos_offset, this_i_theta, this_pos_batch, prj,
                probe_pos_correction, this_ind_batch):

        device_obj = self.common_vars['device_obj']
        lmbda_nm = self.common_vars['lmbda_nm']
        voxel_nm = self.common_vars['voxel_nm']
        probe_size = self.common_vars['probe_size']
        fresnel_approx = self.common_vars['fresnel_approx']
        two_d_mode = self.common_vars['two_d_mode']
        minibatch_size = self.common_vars['minibatch_size']
        ds_level = self.common_vars['ds_level']
        this_obj_size = self.common_vars['this_obj_size']
        energy_ev = self.common_vars['energy_ev']
        psize_cm = self.common_vars['psize_cm']
        h = self.common_vars['h']
        pure_projection = self.common_vars['pure_projection']
        n_dp_batch = self.common_vars['n_dp_batch']
        free_prop_cm = self.common_vars['free_prop_cm']
        optimize_probe_defocusing = self.common_vars['optimize_probe_defocusing']
        optimize_probe_pos_offset = self.common_vars['optimize_probe_pos_offset']
        optimize_all_probe_pos = self.common_vars['optimize_all_probe_pos']
        debug = self.common_vars['debug']
        output_folder = self.common_vars['output_folder']
        unknown_type = self.common_vars['unknown_type']
        n_probe_modes = self.common_vars['n_probe_modes']
        n_theta = self.common_vars['n_theta']
        precalculate_rotation_coords = self.common_vars['precalculate_rotation_coords']
        theta_ls = self.common_vars['theta_ls']

        if precalculate_rotation_coords:
            coord_ls = read_origin_coords('arrsize_{}_{}_{}_ntheta_{}'.format(*this_obj_size, n_theta),
                                          this_i_theta, reverse=False)

        # Allocate subbatches.
        probe_pos_batch_ls = []
        i_dp = 0
        while i_dp < minibatch_size:
            probe_pos_batch_ls.append(this_pos_batch[i_dp:min([i_dp + n_dp_batch, minibatch_size])])
            i_dp += n_dp_batch

        this_pos_batch = np.round(this_pos_batch).astype(int)
        if optimize_probe_defocusing:
            h_probe = get_kernel(probe_defocus_mm * 1e6, lmbda_nm, voxel_nm, probe_size, fresnel_approx=fresnel_approx)
            h_probe_real, h_probe_imag = w.real(h_probe), w.imag(h_probe)
            probe_real, probe_imag = w.convolve_with_transfer_function(probe_real, probe_imag, h_probe_real,
                                                                       h_probe_imag)

        if optimize_probe_pos_offset:
            this_offset = probe_pos_offset[this_i_theta]
            probe_real, probe_imag = realign_image_fourier(probe_real, probe_imag, this_offset, axes=(0, 1), device=device_obj)

        if not two_d_mode and not self.distribution_mode:
            if not self.rotate_out_of_loop:
                if precalculate_rotation_coords:
                    obj_rot = apply_rotation(obj, coord_ls, device=device_obj)
                else:
                    raise NotImplementedError('Rotate on the fly is not yet implemented for non-shared-file mode.')
            else:
                obj_rot = obj
        else:
            obj_rot = obj
        ex_real_ls = []
        ex_imag_ls = []

        # Pad if needed
        if self.distribution_mode is None:
            obj_rot, pad_arr = pad_object(obj_rot, this_obj_size, this_pos_batch, probe_size, unknown_type=unknown_type)

        pos_ind = 0
        for k, pos_batch in enumerate(probe_pos_batch_ls):
            subobj_ls = []
            probe_real_ls = []
            probe_imag_ls = []

            # Get shifted probe list.
            for j in range(len(pos_batch)):
                if optimize_all_probe_pos or len(w.nonzero(probe_pos_correction > 1e-3)) > 0:
                    this_shift = probe_pos_correction[this_i_theta, this_ind_batch[k * n_dp_batch + j]]
                    probe_real_shifted, probe_imag_shifted = realign_image_fourier(probe_real, probe_imag,
                                                                                   this_shift, axes=(1, 2),
                                                                                   device=device_obj)
                    probe_real_ls.append(probe_real_shifted)
                    probe_imag_ls.append(probe_imag_shifted)
            if optimize_all_probe_pos or len(w.nonzero(probe_pos_correction > 1e-3)) > 0:
                # Shape of probe_xxx_ls.shape is [n_dp_batch, n_probe_modes, y, x].
                probe_real_ls = w.stack(probe_real_ls)
                probe_imag_ls = w.stack(probe_imag_ls)
            else:
                # Shape of probe_xxx_ls.shape is [n_probe_modes, y, x].
                probe_real_ls = probe_real
                probe_imag_ls = probe_imag

            # Get object list.
            if not self.distribution_mode:
                if len(pos_batch) == 1:
                    pos = pos_batch[0]
                    pos_y = pos[0] + pad_arr[0, 0]
                    pos_x = pos[1] + pad_arr[1, 0]
                    if pos_y == 0 and pos_x == 0 and probe_size[0] == this_obj_size[0] and probe_size[1] == this_obj_size[1]:
                        subobj = obj_rot
                    else:
                        subobj = obj_rot[pos_y:pos_y + probe_size[0], pos_x:pos_x + probe_size[1], :, :]
                    subobj_ls = w.reshape(subobj, [1, *subobj.shape])
                else:
                    for j in range(len(pos_batch)):
                        pos = pos_batch[j]
                        pos_y = pos[0] + pad_arr[0, 0]
                        pos_x = pos[1] + pad_arr[1, 0]
                        subobj = obj_rot[pos_y:pos_y + probe_size[0], pos_x:pos_x + probe_size[1], :, :]
                        subobj_ls.append(subobj)
                    subobj_ls = w.stack(subobj_ls)
            else:
                subobj_ls = obj[pos_ind:pos_ind + len(pos_batch), :, :, :, :]
                pos_ind += len(pos_batch)

            gc.collect()
            if n_probe_modes == 1:
                if len(probe_real_ls.shape) == 3:
                    this_probe_real_ls = probe_real_ls[0, :, :]
                    this_probe_imag_ls = probe_imag_ls[0, :, :]
                else:
                    this_probe_real_ls = probe_real_ls[:, 0, :, :]
                    this_probe_imag_ls = probe_imag_ls[:, 0, :, :]
                ex_real, ex_imag = multislice_propagate_batch(
                                subobj_ls[:, :, :, :, 0], subobj_ls[:, :, :, :, 1],
                                this_probe_real_ls, this_probe_imag_ls,
                                energy_ev, psize_cm * ds_level, kernel=h, free_prop_cm=free_prop_cm,
                                obj_batch_shape=[len(pos_batch), *probe_size, this_obj_size[-1]],
                                fresnel_approx=fresnel_approx, pure_projection=pure_projection, device=device_obj,
                                type=unknown_type, normalize_fft=self.normalize_fft, sign_convention=self.sign_convention)
                ex_real = w.reshape(ex_real, [len(pos_batch), 1, *probe_size])
                ex_imag = w.reshape(ex_imag, [len(pos_batch), 1, *probe_size])
            else:
                ex_real = []
                ex_imag = []
                for i_mode in range(n_probe_modes):
                    if len(probe_real_ls.shape) == 3:
                        this_probe_real_ls = probe_real_ls[i_mode, :, :]
                        this_probe_imag_ls = probe_imag_ls[i_mode, :, :]
                    else:
                        this_probe_real_ls = probe_real_ls[:, i_mode, :, :]
                        this_probe_imag_ls = probe_imag_ls[:, i_mode, :, :]
                    temp_real, temp_imag = multislice_propagate_batch(
                                subobj_ls[:, :, :, :, 0], subobj_ls[:, :, :, :, 1],
                                this_probe_real_ls, this_probe_imag_ls,
                                energy_ev, psize_cm * ds_level, kernel=h, free_prop_cm=free_prop_cm,
                                obj_batch_shape=[len(pos_batch), *probe_size, this_obj_size[-1]],
                                fresnel_approx=fresnel_approx, pure_projection=pure_projection, device=device_obj,
                                type=unknown_type, normalize_fft=self.normalize_fft, sign_convention=self.sign_convention)
                    ex_real.append(temp_real)
                    ex_imag.append(temp_imag)
                ex_real = w.swap_axes(w.stack(ex_real), [0, 1])
                ex_imag = w.swap_axes(w.stack(ex_imag), [0, 1])
            ex_real_ls.append(ex_real)
            ex_imag_ls.append(ex_imag)
        del subobj_ls, probe_real_ls, probe_imag_ls

        # Output shape is [minibatch_size, n_probe_modes, y, x].
        if len(ex_real_ls) == 1:
            ex_real_ls = ex_real_ls[0]
            ex_imag_ls = ex_imag_ls[0]
        else:
            ex_real_ls = w.concatenate(ex_real_ls, 0)
            ex_imag_ls = w.concatenate(ex_imag_ls, 0)
        if rank == 0 and debug and self.i_call % 10 == 0:
            ex_real_val = w.to_numpy(ex_real_ls)
            ex_imag_val = w.to_numpy(ex_imag_ls)
            dxchange.write_tiff(np.sum(ex_real_val ** 2 + ex_imag_val ** 2, axis=1),
                                os.path.join(output_folder, 'intermediate', 'detected_intensity'), dtype='float32', overwrite=True)
            # dxchange.write_tiff(np.sqrt(ex_real_val ** 2 + ex_imag_val ** 2), os.path.join(output_folder, 'intermediate', 'detected_mag'), dtype='float32', overwrite=True)
            # dxchange.write_tiff(np.arctan2(ex_real_val, ex_imag_val), os.path.join(output_folder, 'intermediate', 'detected_phase'), dtype='float32', overwrite=True)
        self.i_call += 1
        return ex_real_ls, ex_imag_ls

    def get_loss_function(self):
        def calculate_loss(obj, probe_real, probe_imag, probe_defocus_mm,
                           probe_pos_offset, this_i_theta, this_pos_batch, prj,
                           probe_pos_correction, this_ind_batch):
            t0 = time.time()
            ex_real_ls, ex_imag_ls = self.predict(obj, probe_real, probe_imag, probe_defocus_mm,
                           probe_pos_offset, this_i_theta, this_pos_batch, prj,
                           probe_pos_correction, this_ind_batch)
            this_pred_batch = w.norm(ex_real_ls, ex_imag_ls)
            if self.common_vars['n_probe_modes'] == 1:
                this_pred_batch = this_pred_batch[:, 0, :, :]
            else:
                this_pred_batch = w.sqrt(w.sum(this_pred_batch ** 2, axis=1))

            beamstop = self.common_vars['beamstop']
            ds_level = self.common_vars['ds_level']
            theta_downsample = self.common_vars['theta_downsample']
            if theta_downsample is None: theta_downsample = 1

            this_prj_batch = prj[this_i_theta * theta_downsample, this_ind_batch]
            this_prj_batch = w.create_variable(abs(this_prj_batch), requires_grad=False, device=self.device)
            if ds_level > 1:
                this_prj_batch = this_prj_batch[:, ::ds_level, ::ds_level]

            if beamstop is not None:
                beamstop_mask, beamstop_value = beamstop
                beamstop_mask[beamstop_mask >= 1e-5] = 1
                beamstop_mask[beamstop_mask < 1e-5] = 0
                beamstop_mask = w.cast(beamstop_mask, 'bool')
                beamstop_mask_stack = w.tile(beamstop_mask, [len(ex_real_ls), 1, 1])
                this_pred_batch = w.reshape(this_pred_batch[beamstop_mask_stack], [beamstop_mask_stack.shape[0], -1])
                this_prj_batch = w.reshape(this_prj_batch[beamstop_mask_stack], [beamstop_mask_stack.shape[0], -1])
                print_flush('  {} valid pixels remain after applying beamstop mask.'.format(ex_real_ls.shape[1]), 0,
                            rank)
            if self.loss_function_type == 'lsq':
                if self.raw_data_type == 'magnitude':
                    loss = w.mean((this_pred_batch - w.abs(this_prj_batch)) ** 2)
                elif self.raw_data_type == 'intensity':
                    loss = w.mean((this_pred_batch - w.sqrt(w.abs(this_prj_batch))) ** 2)
            elif self.loss_function_type == 'poisson':
                if self.raw_data_type == 'magnitude':
                    loss = w.mean(this_pred_batch ** 2 - w.abs(this_prj_batch) ** 2 * w.log(this_pred_batch ** 2))
                elif self.raw_data_type == 'intensity':
                    loss = w.mean(this_pred_batch ** 2 - w.abs(this_prj_batch) * w.log(this_pred_batch ** 2))
            loss = loss + self.get_regularization_value(obj)
            self.current_loss = float(w.to_numpy(loss))
            del ex_real_ls, ex_imag_ls
            del this_prj_batch
            return loss
        return calculate_loss


class SparseMultisliceModel(ForwardModel):

    def __init__(self, loss_function_type='lsq', distribution_mode=None, device=None, common_vars_dict=None, raw_data_type='magnitude'):
        super(SparseMultisliceModel, self).__init__(loss_function_type, distribution_mode, device, common_vars_dict, raw_data_type)
        # ==========================================================================================
        # argument_ls must be in the same order as arguments in get_loss_function's function call!
        # ==========================================================================================
        self.argument_ls = ['obj', 'probe_real', 'probe_imag', 'probe_defocus_mm',
                            'probe_pos_offset', 'this_i_theta', 'this_pos_batch', 'prj',
                            'probe_pos_correction', 'this_ind_batch', 'slice_pos_cm_ls']

    def predict(self, obj, probe_real, probe_imag, probe_defocus_mm,
                probe_pos_offset, this_i_theta, this_pos_batch, prj,
                probe_pos_correction, this_ind_batch, slice_pos_cm_ls):

        device_obj = self.common_vars['device_obj']
        lmbda_nm = self.common_vars['lmbda_nm']
        voxel_nm = self.common_vars['voxel_nm']
        probe_size = self.common_vars['probe_size']
        fresnel_approx = self.common_vars['fresnel_approx']
        two_d_mode = self.common_vars['two_d_mode']
        minibatch_size = self.common_vars['minibatch_size']
        ds_level = self.common_vars['ds_level']
        this_obj_size = self.common_vars['this_obj_size']
        energy_ev = self.common_vars['energy_ev']
        psize_cm = self.common_vars['psize_cm']
        h = self.common_vars['h']
        pure_projection = self.common_vars['pure_projection']
        n_dp_batch = self.common_vars['n_dp_batch']
        free_prop_cm = self.common_vars['free_prop_cm']
        optimize_probe_defocusing = self.common_vars['optimize_probe_defocusing']
        optimize_probe_pos_offset = self.common_vars['optimize_probe_pos_offset']
        optimize_all_probe_pos = self.common_vars['optimize_all_probe_pos']
        debug = self.common_vars['debug']
        output_folder = self.common_vars['output_folder']
        unknown_type = self.common_vars['unknown_type']
        n_probe_modes = self.common_vars['n_probe_modes']
        n_theta = self.common_vars['n_theta']
        precalculate_rotation_coords = self.common_vars['precalculate_rotation_coords']
        theta_ls = self.common_vars['theta_ls']
        u = self.common_vars['u']
        v = self.common_vars['v']

        if precalculate_rotation_coords:
            coord_ls = read_origin_coords('arrsize_{}_{}_{}_ntheta_{}'.format(*this_obj_size, n_theta),
                                          this_i_theta, reverse=False)

        # Allocate subbatches.
        probe_pos_batch_ls = []
        i_dp = 0
        while i_dp < minibatch_size:
            probe_pos_batch_ls.append(this_pos_batch[i_dp:min([i_dp + n_dp_batch, minibatch_size])])
            i_dp += n_dp_batch

        this_pos_batch = np.round(this_pos_batch).astype(int)
        if optimize_probe_defocusing:
            h_probe = get_kernel(probe_defocus_mm * 1e6, lmbda_nm, voxel_nm, probe_size, fresnel_approx=fresnel_approx)
            h_probe_real, h_probe_imag = w.real(h_probe), w.imag(h_probe)
            probe_real, probe_imag = w.convolve_with_transfer_function(probe_real, probe_imag, h_probe_real,
                                                                       h_probe_imag)

        if optimize_probe_pos_offset:
            this_offset = probe_pos_offset[this_i_theta]
            probe_real, probe_imag = realign_image_fourier(probe_real, probe_imag, this_offset, axes=(0, 1), device=device_obj)

        if not two_d_mode and not self.distribution_mode:
            if not self.rotate_out_of_loop:
                if precalculate_rotation_coords:
                    obj_rot = apply_rotation(obj, coord_ls, device=device_obj)
                else:
                    raise NotImplementedError('Rotate on the fly is not yet implemented for non-shared-file mode.')
            else:
                obj_rot = obj
        else:
            obj_rot = obj
        ex_real_ls = []
        ex_imag_ls = []

        # Pad if needed
        if not self.distribution_mode:
            obj_rot, pad_arr = pad_object(obj_rot, this_obj_size, this_pos_batch, probe_size, unknown_type=unknown_type)

        pos_ind = 0
        for k, pos_batch in enumerate(probe_pos_batch_ls):
            subobj_ls = []
            probe_real_ls = []
            probe_imag_ls = []

            # Get shifted probe list.
            for j in range(len(pos_batch)):
                if optimize_all_probe_pos or len(w.nonzero(probe_pos_correction > 1e-3)) > 0:
                    this_shift = probe_pos_correction[this_i_theta, this_ind_batch[k * n_dp_batch + j]]
                    probe_real_shifted, probe_imag_shifted = realign_image_fourier(probe_real, probe_imag,
                                                                                   this_shift, axes=(1, 2),
                                                                                   device=device_obj)
                    probe_real_ls.append(probe_real_shifted)
                    probe_imag_ls.append(probe_imag_shifted)
            if optimize_all_probe_pos or len(w.nonzero(probe_pos_correction > 1e-3)) > 0:
                # Shape of probe_xxx_ls.shape is [n_dp_batch, n_probe_modes, y, x].
                probe_real_ls = w.stack(probe_real_ls)
                probe_imag_ls = w.stack(probe_imag_ls)
            else:
                # Shape of probe_xxx_ls.shape is [n_probe_modes, y, x].
                probe_real_ls = probe_real
                probe_imag_ls = probe_imag

            # Get object list.
            if not self.distribution_mode:
                if len(pos_batch) == 1:
                    pos = pos_batch[0]
                    pos_y = pos[0] + pad_arr[0, 0]
                    pos_x = pos[1] + pad_arr[1, 0]
                    subobj = obj_rot[pos_y:pos_y + probe_size[0], pos_x:pos_x + probe_size[1], :, :]
                    subobj_ls = w.reshape(subobj, [1, *subobj.shape])
                else:
                    for j in range(len(pos_batch)):
                        pos = pos_batch[j]
                        pos_y = pos[0] + pad_arr[0, 0]
                        pos_x = pos[1] + pad_arr[1, 0]
                        subobj = obj_rot[pos_y:pos_y + probe_size[0], pos_x:pos_x + probe_size[1], :, :]
                        subobj_ls.append(subobj)
                    subobj_ls = w.stack(subobj_ls)
            else:
                subobj_ls = obj[pos_ind:pos_ind + len(pos_batch), :, :, :, :]
                pos_ind += len(pos_batch)

            gc.collect()
            if n_probe_modes == 1:
                if len(probe_real_ls.shape) == 3:
                    this_probe_real_ls = probe_real_ls[0, :, :]
                    this_probe_imag_ls = probe_imag_ls[0, :, :]
                else:
                    this_probe_real_ls = probe_real_ls[:, 0, :, :]
                    this_probe_imag_ls = probe_imag_ls[:, 0, :, :]
                ex_real, ex_imag = sparse_multislice_propagate_batch(u, v,
                                *w.split_channel(subobj_ls), this_probe_real_ls,
                                this_probe_imag_ls, energy_ev, psize_cm * ds_level, slice_pos_cm_ls, free_prop_cm=free_prop_cm,
                                obj_batch_shape=[len(pos_batch), *probe_size, this_obj_size[-1]],
                                fresnel_approx=fresnel_approx, device=device_obj,
                                type=unknown_type, normalize_fft=self.normalize_fft, sign_convention=self.sign_convention)
                ex_real = w.reshape(ex_real, [len(pos_batch), 1, *probe_size])
                ex_imag = w.reshape(ex_imag, [len(pos_batch), 1, *probe_size])
            else:
                ex_real = []
                ex_imag = []
                for i_mode in range(n_probe_modes):
                    if len(probe_real_ls.shape) == 3:
                        this_probe_real_ls = probe_real_ls[i_mode, :, :]
                        this_probe_imag_ls = probe_imag_ls[i_mode, :, :]
                    else:
                        this_probe_real_ls = probe_real_ls[:, i_mode, :, :]
                        this_probe_imag_ls = probe_imag_ls[:, i_mode, :, :]
                    temp_real, temp_imag = sparse_multislice_propagate_batch(
                                u, v, *w.split_channel(subobj_ls),
                                this_probe_real_ls, this_probe_imag_ls,
                                energy_ev, psize_cm * ds_level, slice_pos_cm_ls, free_prop_cm=free_prop_cm,
                                obj_batch_shape=[len(pos_batch), *probe_size, this_obj_size[-1]],
                                fresnel_approx=fresnel_approx, device=device_obj,
                                type=unknown_type, normalize_fft=self.normalize_fft, sign_convention=self.sign_convention)
                    ex_real.append(temp_real)
                    ex_imag.append(temp_imag)
                ex_real = w.swap_axes(w.stack(ex_real), [0, 1])
                ex_imag = w.swap_axes(w.stack(ex_imag), [0, 1])
            ex_real_ls.append(ex_real)
            ex_imag_ls.append(ex_imag)
        del subobj_ls, probe_real_ls, probe_imag_ls

        # Output shape is [minibatch_size, n_probe_modes, y, x].
        ex_real_ls = w.concatenate(ex_real_ls, 0)
        ex_imag_ls = w.concatenate(ex_imag_ls, 0)

        if rank == 0 and debug and self.i_call % 10 == 0:
            ex_real_val = w.to_numpy(ex_real_ls)
            ex_imag_val = w.to_numpy(ex_imag_ls)
            dxchange.write_tiff(np.sum(ex_real_val ** 2 + ex_imag_val ** 2, axis=1),
                                os.path.join(output_folder, 'intermediate', 'detected_intensity'), dtype='float32', overwrite=True)
            # dxchange.write_tiff(np.sqrt(ex_real_val ** 2 + ex_imag_val ** 2), os.path.join(output_folder, 'intermediate', 'detected_mag'), dtype='float32', overwrite=True)
            # dxchange.write_tiff(np.arctan2(ex_real_val, ex_imag_val), os.path.join(output_folder, 'intermediate', 'detected_phase'), dtype='float32', overwrite=True)
        self.i_call += 1
        return ex_real_ls, ex_imag_ls

    def get_loss_function(self):
        def calculate_loss(obj, probe_real, probe_imag, probe_defocus_mm,
                           probe_pos_offset, this_i_theta, this_pos_batch, prj,
                           probe_pos_correction, this_ind_batch, slice_pos_cm_ls):
            ex_real_ls, ex_imag_ls = self.predict(obj, probe_real, probe_imag, probe_defocus_mm,
                           probe_pos_offset, this_i_theta, this_pos_batch, prj,
                           probe_pos_correction, this_ind_batch, slice_pos_cm_ls)
            this_pred_batch = w.norm(ex_real_ls, ex_imag_ls)
            if self.common_vars['n_probe_modes'] == 1:
                this_pred_batch = this_pred_batch[:, 0, :, :]
            else:
                this_pred_batch = w.sqrt(w.sum(this_pred_batch ** 2, axis=1))

            beamstop = self.common_vars['beamstop']
            ds_level = self.common_vars['ds_level']
            theta_downsample = self.common_vars['theta_downsample']
            if theta_downsample is None: theta_downsample = 1

            this_prj_batch = prj[this_i_theta * theta_downsample, this_ind_batch]
            this_prj_batch = w.create_variable(abs(this_prj_batch), requires_grad=False, device=self.device)
            if ds_level > 1:
                this_prj_batch = this_prj_batch[:, ::ds_level, ::ds_level]

            if beamstop is not None:
                beamstop_mask, beamstop_value = beamstop
                beamstop_mask[beamstop_mask >= 1e-5] = 1
                beamstop_mask[beamstop_mask < 1e-5] = 0
                beamstop_mask = w.cast(beamstop_mask, 'bool')
                beamstop_mask_stack = w.tile(beamstop_mask, [len(ex_real_ls), 1, 1])
                this_pred_batch = w.reshape(this_pred_batch[beamstop_mask_stack], [beamstop_mask_stack.shape[0], -1])
                this_prj_batch = w.reshape(this_prj_batch[beamstop_mask_stack], [beamstop_mask_stack.shape[0], -1])
                print_flush('  {} valid pixels remain after applying beamstop mask.'.format(ex_real_ls.shape[1]), 0,
                            rank)

            if self.loss_function_type == 'lsq':
                if self.raw_data_type == 'magnitude':
                    loss = w.mean((this_pred_batch - w.abs(this_prj_batch)) ** 2)
                elif self.raw_data_type == 'intensity':
                    loss = w.mean((this_pred_batch - w.sqrt(w.abs(this_prj_batch))) ** 2)
            elif self.loss_function_type == 'poisson':
                if self.raw_data_type == 'magnitude':
                    loss = w.mean(this_pred_batch ** 2 - w.abs(this_prj_batch) ** 2 * w.log(this_pred_batch ** 2))
                elif self.raw_data_type == 'intensity':
                    loss = w.mean(this_pred_batch ** 2 - w.abs(this_prj_batch) * w.log(this_pred_batch ** 2))
            loss = loss + self.get_regularization_value(obj)
            self.current_loss = float(w.to_numpy(loss))
            del ex_real_ls, ex_imag_ls
            del this_prj_batch
            return loss
        return calculate_loss


class MultiDistModel(ForwardModel):

    def __init__(self, loss_function_type='lsq', distribution_mode=None, device=None, common_vars_dict=None, raw_data_type='magnitude'):
        super(MultiDistModel, self).__init__(loss_function_type, distribution_mode, device, common_vars_dict, raw_data_type)
        # ==========================================================================================
        # argument_ls must be in the same order as arguments in get_loss_function's function call!
        # ==========================================================================================
        self.argument_ls = ['obj', 'probe_real', 'probe_imag', 'probe_defocus_mm',
                            'probe_pos_offset', 'this_i_theta', 'this_pos_batch', 'prj',
                            'probe_pos_correction', 'this_ind_batch', 'free_prop_cm', 'safe_zone_width']

    def predict(self, obj, probe_real, probe_imag, probe_defocus_mm,
                probe_pos_offset, this_i_theta, this_pos_batch, prj,
                probe_pos_correction, this_ind_batch, free_prop_cm, safe_zone_width):

        device_obj = self.common_vars['device_obj']
        lmbda_nm = self.common_vars['lmbda_nm']
        voxel_nm = self.common_vars['voxel_nm']
        probe_size = self.common_vars['probe_size']
        subprobe_size = self.common_vars['subprobe_size']
        fresnel_approx = self.common_vars['fresnel_approx']
        two_d_mode = self.common_vars['two_d_mode']
        minibatch_size = self.common_vars['minibatch_size']
        ds_level = self.common_vars['ds_level']
        this_obj_size = self.common_vars['this_obj_size']
        energy_ev = self.common_vars['energy_ev']
        psize_cm = self.common_vars['psize_cm']
        h = self.common_vars['h']
        pure_projection = self.common_vars['pure_projection']
        n_dp_batch = self.common_vars['n_dp_batch']
        optimize_probe_defocusing = self.common_vars['optimize_probe_defocusing']
        optimize_probe_pos_offset = self.common_vars['optimize_probe_pos_offset']
        optimize_all_probe_pos = self.common_vars['optimize_all_probe_pos']
        optimize_free_prop = self.common_vars['optimize_free_prop']
        debug = self.common_vars['debug']
        output_folder = self.common_vars['output_folder']
        unknown_type = self.common_vars['unknown_type']
        beamstop = self.common_vars['beamstop']
        n_probe_modes = self.common_vars['n_probe_modes']
        n_theta = self.common_vars['n_theta']
        precalculate_rotation_coords = self.common_vars['precalculate_rotation_coords']
        u_free = self.common_vars['u_free']
        v_free = self.common_vars['v_free']

        if precalculate_rotation_coords:
            coord_ls = read_origin_coords('arrsize_{}_{}_{}_ntheta_{}'.format(*this_obj_size, n_theta),
                                          this_i_theta, reverse=False)

        n_dists = len(free_prop_cm)
        n_blocks = prj.shape[1] // n_dists

        this_pos_batch = np.round(this_pos_batch).astype(int)
        if optimize_probe_defocusing:
            h_probe = get_kernel(probe_defocus_mm * 1e6, lmbda_nm, voxel_nm, probe_size, fresnel_approx=fresnel_approx)
            h_probe_real, h_probe_imag = w.real(h_probe), w.imag(h_probe)
            probe_real, probe_imag = w.convolve_with_transfer_function(probe_real, probe_imag, h_probe_real,
                                                                       h_probe_imag)
        # Allocate subbatches.
        probe_pos_batch_ls = []
        i_dp = 0
        while i_dp < minibatch_size:
            probe_pos_batch_ls.append(this_pos_batch[i_dp:min([i_dp + n_dp_batch, minibatch_size])])
            i_dp += n_dp_batch

        if not two_d_mode:
            if not self.rotate_out_of_loop:
                if precalculate_rotation_coords:
                    obj_rot = apply_rotation(obj, coord_ls, device=device_obj)
                else:
                    raise NotImplementedError('Rotate on the fly is not yet implemented for non-shared-file mode.')
            else:
                obj_rot = obj
        else:
            obj_rot = obj

        # Pad object with safe zone width if not using low-mem mode (chunks will be read padded otherwise).
        szw_arr = np.array([safe_zone_width] * 2)
        if not self.distribution_mode:
            obj_rot, pad_arr = pad_object(obj_rot, this_obj_size, this_pos_batch - szw_arr, subprobe_size + 2 * szw_arr, unknown_type=unknown_type)

        # Pad probe with safe zone width.
        if safe_zone_width > 0:
            pad_arr = calculate_pad_len(probe_size, this_pos_batch - szw_arr, subprobe_size + 2 * szw_arr, unknown_type=unknown_type)
            probe_real_sz = w.pad(probe_real, [[0, 0]] + pad_arr, mode='constant', constant_values=1)
            probe_imag_sz = w.pad(probe_imag, [[0, 0]] + pad_arr, mode='constant', constant_values=0)
        else:
            probe_real_sz = probe_real
            probe_imag_sz = probe_imag
            pad_arr = np.array([[0, 0], [0, 0]])

        subobj_ls_ls = []
        subprobe_real_ls_ls = []
        subprobe_imag_ls_ls = []
        pos_ind = 0
        for k, pos_batch in enumerate(probe_pos_batch_ls):
            if self.distribution_mode is None:
                if n_blocks > 1:
                    subobj_subbatch_ls = []
                    subprobe_subbatch_real_ls = []
                    subprobe_subbatch_imag_ls = []
                    if len(pos_batch) == 1:
                        pos = pos_batch[0]
                        pos_y = pos[0] + pad_arr[0, 0]
                        pos_x = pos[1] + pad_arr[1, 0]
                        subobj = obj_rot[pos_y:pos_y + probe_size[0], pos_x:pos_x + probe_size[1], :, :]
                        subobj_subbatch_ls = w.reshape(subobj, [1, *subobj.shape])
                        sub_probe_real = probe_real_sz[:, pos_y:pos_y + subprobe_size[0] + safe_zone_width * 2,
                                                        pos_x:pos_x + subprobe_size[1] + safe_zone_width * 2]
                        sub_probe_imag = probe_imag_sz[:, pos_y:pos_y + subprobe_size[0] + safe_zone_width * 2,
                                                        pos_x:pos_x + subprobe_size[1] + safe_zone_width * 2]
                        subprobe_subbatch_real_ls = w.reshape(sub_probe_real, [1, *sub_probe_real.shape])
                        subprobe_subbatch_imag_ls = w.reshape(sub_probe_imag, [1, *sub_probe_imag.shape])

                    else:
                        for j in range(len(pos_batch)):
                            pos = pos_batch[j]
                            pos_y = pos[0] + pad_arr[0, 0] - safe_zone_width
                            pos_x = pos[1] + pad_arr[1, 0] - safe_zone_width
                            subobj = obj_rot[pos_y:pos_y + subprobe_size[0] + safe_zone_width * 2,
                                             pos_x:pos_x + subprobe_size[1] + safe_zone_width * 2, :, :]
                            sub_probe_real = probe_real_sz[:, pos_y:pos_y + subprobe_size[0] + safe_zone_width * 2,
                                                              pos_x:pos_x + subprobe_size[1] + safe_zone_width * 2]
                            sub_probe_imag = probe_imag_sz[:, pos_y:pos_y + subprobe_size[0] + safe_zone_width * 2,
                                                              pos_x:pos_x + subprobe_size[1] + safe_zone_width * 2]
                            subobj_subbatch_ls.append(subobj)
                            subprobe_subbatch_real_ls.append(sub_probe_real)
                            subprobe_subbatch_imag_ls.append(sub_probe_imag)
                        subobj_subbatch_ls = w.stack(subobj_subbatch_ls)
                        subprobe_subbatch_real_ls = w.stack(subprobe_subbatch_real_ls)
                        subprobe_subbatch_imag_ls = w.stack(subprobe_subbatch_imag_ls)
                else:
                    subobj_subbatch_ls = w.reshape(obj_rot, [1, *obj_rot.shape])
                    subprobe_subbatch_real_ls = w.reshape(probe_real_sz, [1, *probe_real_sz.shape])
                    subprobe_subbatch_imag_ls = w.reshape(probe_imag_sz, [1, *probe_imag_sz.shape])
                # Shape of subprobe_real_ls_ls is [n_subbatches, len(pos_batch), y, x, z].
                subobj_ls_ls.append(subobj_subbatch_ls)
            else:
                subobj_ls_ls.append(obj[pos_ind:pos_ind + len(pos_batch)])
                for j in range(len(pos_batch)):
                    pos = pos_batch[j]
                    pos_y = pos[0] + pad_arr[0, 0] - safe_zone_width
                    pos_x = pos[1] + pad_arr[1, 0] - safe_zone_width
                    sub_probe_real = probe_real_sz[:, pos_y:pos_y + subprobe_size[0] + safe_zone_width * 2,
                                     pos_x:pos_x + subprobe_size[1] + safe_zone_width * 2]
                    sub_probe_imag = probe_imag_sz[:, pos_y:pos_y + subprobe_size[0] + safe_zone_width * 2,
                                     pos_x:pos_x + subprobe_size[1] + safe_zone_width * 2]
                    subprobe_subbatch_real_ls.append(sub_probe_real)
                    subprobe_subbatch_imag_ls.append(sub_probe_imag)
                subprobe_subbatch_real_ls = w.stack(subprobe_subbatch_real_ls)
                subprobe_subbatch_imag_ls = w.stack(subprobe_subbatch_imag_ls)

            # Shape of subprobe_real_ls_ls is [n_subbatches, len(pos_batch), n_probe_modes, y, x].
            subprobe_real_ls_ls.append(subprobe_subbatch_real_ls)
            subprobe_imag_ls_ls.append(subprobe_subbatch_imag_ls)
            pos_ind += len(pos_batch)

        ex_real_ls = []
        ex_imag_ls = []
        for i_dist, this_dist in enumerate(free_prop_cm):
            for k, pos_batch in enumerate(probe_pos_batch_ls):
                ex_real = []
                ex_imag = []
                for i_mode in range(n_probe_modes):
                    temp_real, temp_imag = multislice_propagate_batch(
                        *w.split_channel(subobj_ls_ls[k]),
                        subprobe_real_ls_ls[k][:, i_mode, :, :], subprobe_imag_ls_ls[k][i_mode, :, :],
                        energy_ev, psize_cm * ds_level, kernel=h, free_prop_cm=this_dist,
                        obj_batch_shape=[len(pos_batch), subprobe_size[0] + 2 * safe_zone_width, subprobe_size[1] + 2 * safe_zone_width, this_obj_size[-1]],
                        fresnel_approx=fresnel_approx, pure_projection=pure_projection, device=device_obj,
                        type=unknown_type, sign_convention=self.sign_convention, optimize_free_prop=optimize_free_prop,
                        u_free=u_free, v_free=v_free)
                    ex_real.append(temp_real)
                    ex_imag.append(temp_imag)
                ex_real = w.swap_axes(w.stack(ex_real), [0, 1])
                ex_imag = w.swap_axes(w.stack(ex_imag), [0, 1])
            ex_real_ls.append(ex_real)
            ex_imag_ls.append(ex_imag)
        # Output shape is [minibatch_size, n_probe_modes, y, x].
        ex_real_ls = w.concatenate(ex_real_ls)
        ex_imag_ls = w.concatenate(ex_imag_ls)
        if safe_zone_width > 0:
            ex_real_ls = ex_real_ls[:, :, safe_zone_width:safe_zone_width + subprobe_size[0],
                      safe_zone_width:safe_zone_width + subprobe_size[1]]
            ex_real_ls = ex_real_ls[:, :, safe_zone_width:safe_zone_width + subprobe_size[0],
                      safe_zone_width:safe_zone_width + subprobe_size[1]]

        if rank == 0 and debug:
            ex_real_val = w.to_numpy(ex_real_ls)
            ex_imag_val = w.to_numpy(ex_imag_ls)
            dxchange.write_tiff(np.sqrt(ex_real_val ** 2 + ex_imag_val ** 2), os.path.join(output_folder, 'intermediate', 'detected_mag'), dtype='float32', overwrite=True)
            dxchange.write_tiff(np.arctan2(ex_imag_val, ex_real_val), os.path.join(output_folder, 'intermediate', 'detected_phase'), dtype='float32', overwrite=True)

        del subobj_ls_ls, subprobe_real_ls_ls, subprobe_imag_ls_ls
        return ex_real_ls, ex_imag_ls

    def get_loss_function(self):
        def calculate_loss(obj, probe_real, probe_imag, probe_defocus_mm,
                           probe_pos_offset, this_i_theta, this_pos_batch, prj,
                           probe_pos_correction, this_ind_batch, free_prop_cm, safe_zone_width):

            beamstop = self.common_vars['beamstop']
            ds_level = self.common_vars['ds_level']
            optimize_probe_pos_offset = self.common_vars['optimize_probe_pos_offset']
            optimize_all_probe_pos = self.common_vars['optimize_all_probe_pos']
            device_obj = self.common_vars['device_obj']
            minibatch_size =self.common_vars['minibatch_size']
            theta_downsample = self.common_vars['theta_downsample']
            if theta_downsample is None: theta_downsample = 1

            ex_real_ls, ex_imag_ls = self.predict(obj, probe_real, probe_imag, probe_defocus_mm,
                           probe_pos_offset, this_i_theta, this_pos_batch, prj,
                           probe_pos_correction, this_ind_batch, free_prop_cm, safe_zone_width)
            this_pred_batch = w.norm(ex_real_ls, ex_imag_ls)
            if self.common_vars['n_probe_modes'] == 1:
                this_pred_batch = this_pred_batch[:, 0, :, :]
            else:
                this_pred_batch = w.sqrt(w.sum(this_pred_batch ** 2, axis=1))

            n_dists = len(free_prop_cm)
            n_blocks = prj.shape[1] // n_dists
            this_ind_batch_full = this_ind_batch
            for i in range(1, n_dists):
                this_ind_batch_full = np.concatenate([this_ind_batch_full, this_ind_batch + i * n_blocks])
            this_prj_batch = prj[this_i_theta * theta_downsample, this_ind_batch_full]
            this_prj_batch = w.create_variable(abs(this_prj_batch), requires_grad=False, device=self.device)
            if ds_level > 1:
                this_prj_batch = this_prj_batch[:, :ds_level, ::ds_level]

            if optimize_probe_pos_offset:
                this_offset = probe_pos_offset[this_i_theta]
                this_prj_batch, _ = realign_image_fourier(this_prj_batch, w.zeros_like(this_prj_batch), this_offset, axes=(0, 1),
                                                               device=device_obj)

            if optimize_all_probe_pos:
                shifted_prj_ls = []
                for i in range(n_dists):
                    this_shift = probe_pos_correction[i]
                    this_prj_batch_idist = this_prj_batch[len(this_ind_batch) * i:len(this_ind_batch) * (i + 1)]
                    this_prj_batch_idist, _ = realign_image_fourier(this_prj_batch_idist, w.zeros_like(this_prj_batch_idist),
                                                                  this_shift, axes=(1, 2),
                                                                  device=device_obj)
                    shifted_prj_ls.append(this_prj_batch_idist)
                this_prj_batch = w.concatenate(shifted_prj_ls)

            if beamstop is not None:
                beamstop_mask, beamstop_value = beamstop
                beamstop_mask = w.cast(beamstop_mask, 'bool')
                beamstop_mask_stack = w.tile(beamstop_mask, [len(ex_real_ls), 1, 1])
                this_pred_batch = w.reshape(this_pred_batch[beamstop_mask_stack], [beamstop_mask_stack.shape[0], -1])
                this_prj_batch = w.reshape(this_prj_batch[beamstop_mask_stack], [beamstop_mask_stack.shape[0], -1])
                print_flush('  {} valid pixels remain after applying beamstop mask.'.format(ex_real_ls.shape[1]), 0, rank)

            if self.loss_function_type == 'lsq':
                if self.raw_data_type == 'magnitude':
                    loss = w.mean((this_pred_batch - w.abs(this_prj_batch)) ** 2)
                elif self.raw_data_type == 'intensity':
                    loss = w.mean((this_pred_batch - w.sqrt(w.abs(this_prj_batch))) ** 2)
            elif self.loss_function_type == 'poisson':
                if self.raw_data_type == 'magnitude':
                    loss = w.mean(this_pred_batch ** 2 - w.abs(this_prj_batch) ** 2 * w.log(this_pred_batch ** 2))
                elif self.raw_data_type == 'intensity':
                    loss = w.mean(this_pred_batch ** 2 - w.abs(this_prj_batch) * w.log(this_pred_batch ** 2))
            loss = loss + self.get_regularization_value(obj)
            self.current_loss = float(w.to_numpy(loss))

            del ex_real_ls, ex_imag_ls
            del this_prj_batch
            return loss
        return calculate_loss


def l1_norm_term(obj, alpha_d, alpha_b, device=None):
    slicer = [slice(None)] * (len(obj.shape) - 1)
    reg = w.create_variable(0., device=device)
    if alpha_d not in [None, 0]:
        reg = reg + alpha_d * w.mean(w.abs(obj[slicer + [0]]))
    if alpha_b not in [None, 0]:
        reg = reg + alpha_b * w.mean(w.abs(obj[slicer + [1]]))
    return reg

def reweighted_l1_norm_term(obj, alpha_d, alpha_b, weight_l1, device=None):
    slicer = [slice(None)] * (len(obj.shape) - 1)
    reg = w.create_variable(0., device=device)
    if alpha_d not in [None, 0]:
        reg = reg + alpha_d * w.mean(weight_l1 * w.abs(obj[slicer + [0]]))
    if alpha_b not in [None, 0]:
        reg = reg + alpha_b * w.mean(weight_l1 * w.abs(obj[slicer + [1]]))
    return reg

def tv(obj, gamma, distribution_mode, device=None):
    slicer = [slice(None)] * (len(obj.shape) - 1)
    reg = w.create_variable(0., device=device)
    if distribution_mode:
        reg = reg + gamma * total_variation_3d(obj[slicer + [0]], axis_offset=1)
    else:
        reg = reg + gamma * total_variation_3d(obj[slicer + [0]], axis_offset=0)
    return reg
