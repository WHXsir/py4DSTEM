# Functions for calculating diffraction patterns, matching them to experiments, and creating orientation and phase maps.

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.axes import Axes
from mpl_toolkits.mplot3d import Axes3D, art3d
import warnings
from typing import Union, Optional

try:
    import pymatgen as mg
    from pymatgen.ext.matproj import MPRester
except Exception:
    print(r"pymatgen not found... Crystal module won't work ¯\_(ツ)_/¯")

from ...io.datastructure import PointList, PointListArray
from ..utils import tqdmnd, single_atom_scatter, electron_wavelength_angstrom


class Crystal:
    """
    A class storing a single crystal structure, and associated diffraction data.

    Args:
        structure       a pymatgen Structure object for the material
                        or a string containing the Materials Project ID for the 
                        structure (requires API key in config file, see:
                        https://pymatgen.org/usage.html#setting-the-pmg-mapi-key-in-the-config-file
        conventional_standard_structure: (bool) whether to convert primitive unit cell to 
                        conventional structure. When using the MP API, cells are usually primitive
                        and so indexing will be with respect to the primitive basis vectors,
                        which will yield unexpected orientation results. Set to True to 
                        convet the cell to a conventional representation. 
    """

    def __init__(
        self, 
        structure:Union[str,mg.core.Structure],
        conventional_standard_structure:bool=True,
        ):
        """
        Instantiate a Crystal object. 
        Calculate lattice vectors.
        """
        
        if isinstance(structure, str):
            with MPRester() as m:
                structure = m.get_structure_by_material_id(structure)

        assert isinstance(
            structure, mg.core.Structure
        ), "structure must be pymatgen Structure object"


        self.structure = (
            mg.symmetry.analyzer.SpacegroupAnalyzer(
                structure
            ).get_conventional_standard_structure()
            if conventional_standard_structure
            else structure
        )
        self.struc_dict = self.structure.as_dict()

        self.lat_inv = self.structure.lattice.reciprocal_lattice_crystallographic.matrix
        self.lat_real = self.structure.lattice.matrix

        # Initialize Crystal
        self.positions = self.structure.frac_coords   #: fractional atomic coordinates

        #: atomic numbers
        self.numbers = np.array([s.Z for s in self.structure.species], dtype=np.intp)

    def calculate_structure_factors(
        self, 
        k_max:float=2.0, 
        tol_structure_factor:float=1e-2):
        """
        Calculate structure factors for all hkl indices up to max scattering vector k_max
        
        Args:
            k_max (numpy float):                max scattering vector to include (1/Angstroms)
            tol_structure_factor (numpy float): tolerance for removing low-valued structure factors
        """

        # Store k_max
        self.k_max = np.asarray(k_max)

        # Inverse lattice vectors
        lat_inv = np.linalg.inv(self.lat_real)

        # Find shortest lattice vector direction
        k_test = np.vstack([
            lat_inv[0,:],
            lat_inv[1,:],
            lat_inv[2,:],
            lat_inv[0,:] + lat_inv[1,:],
            lat_inv[0,:] + lat_inv[2,:],
            lat_inv[1,:] + lat_inv[2,:],
            lat_inv[0,:] + lat_inv[1,:] + lat_inv[2,:],
            lat_inv[0,:] - lat_inv[1,:] + lat_inv[2,:],
            lat_inv[0,:] + lat_inv[1,:] - lat_inv[2,:],
            lat_inv[0,:] - lat_inv[1,:] - lat_inv[2,:],
            ])
        k_leng_min = np.min(np.linalg.norm(k_test, axis=1))

        # Tile lattice vectors
        num_tile = np.ceil(self.k_max / k_leng_min)
        ya,xa,za = np.meshgrid(
            np.arange(-num_tile, num_tile+1),
            np.arange(-num_tile, num_tile+1),
            np.arange(-num_tile, num_tile+1))
        hkl = np.vstack([xa.ravel(), ya.ravel(), za.ravel()])
        g_vec_all = lat_inv @ hkl

        # Delete lattice vectors outside of k_max
        keep = np.linalg.norm(g_vec_all, axis=0) <= self.k_max
        self.hkl = hkl[:,keep]
        self.g_vec_all = g_vec_all[:,keep]
        self.g_vec_leng = np.linalg.norm(self.g_vec_all, axis=0)

        # Calculate single atom scattering factors
        # Note this can be sped up a lot, but we may want to generalize to allow non-1.0 occupancy in the future.
        f_all = np.zeros((np.size(self.g_vec_leng, 0), self.positions.shape[0]), dtype='float_')
        for a0 in range(self.positions.shape[0]):
            atom_sf = single_atom_scatter(
                [self.numbers[a0]],
                [1],
                self.g_vec_leng,
                'A')
            atom_sf.get_scattering_factor(
                [self.numbers[a0]],
                [1],
                self.g_vec_leng,
                'A')
            f_all[:,a0] = atom_sf.fe

        # Calculate structure factors
        self.struct_factors = np.zeros(np.size(self.g_vec_leng, 0), dtype='complex64')
        for a0 in range(self.positions.shape[0]):
            self.struct_factors += f_all[:,a0] * \
                np.exp((2j * np.pi) * \
                np.sum(self.hkl * np.expand_dims(self.positions[a0,:],axis=1),axis=0))

        # Remove structure factors below tolerance level
        keep = np.abs(self.struct_factors) > tol_structure_factor
        self.hkl = self.hkl[:,keep]
        self.g_vec_all = self.g_vec_all[:,keep]
        self.g_vec_leng = self.g_vec_leng[keep]
        self.struct_factors = self.struct_factors[keep]

        # Structure factor intensities
        self.struct_factors_int = np.abs(self.struct_factors)**2 




    def plot_structure(
        self,
        proj_dir:Union[list,np.ndarray]=[3,2,1],
        size_marker:float=400,
        tol_distance:float=0.001,
        plot_limit:Optional[np.ndarray]=None,
        show_axes:bool=False,
        figsize:Union[tuple,list,np.ndarray]=(8,8),
        returnfig:bool=False):
        """
        Quick 3D plot of the untit cell /atomic structure.

        Args:
            proj_dir (float):           projection direction, either [elev azim] or normal vector
            scale_markers (float):      size scaling for markers
            tol_distance (float):       tolerance for repeating atoms on edges on cell boundaries
            plot_limit (float):         2x3 numpy array containing x y z plot min and max in columns.
                                        Default is 1.1* unit cell dimensions
            show_axes (bool):           Whether to plot axes or not
            figsize (2 element float):  size scaling of figure axes
            returnfig (bool):           set to True to return figure and axes handles

        Returns:
            fig, ax                     (optional) figure and axes handles
        """


        # unit cell vectors
        u = self.lat_real[0,:]
        v = self.lat_real[1,:]
        w = self.lat_real[2,:]

        # atomic identities
        ID = self.numbers

        # Fractional atomic coordinates
        pos = self.positions
        # x tile
        sub = pos[:,0] < tol_distance
        pos = np.vstack([pos,pos[sub,:]+np.array([1,0,0])])
        ID = np.hstack([ID,ID[sub]])
        # y tile
        sub = pos[:,1] < tol_distance
        pos = np.vstack([pos,pos[sub,:]+np.array([0,1,0])])
        ID = np.hstack([ID,ID[sub]])
        # z tile
        sub = pos[:,2] < tol_distance
        pos = np.vstack([pos,pos[sub,:]+np.array([0,0,1])])
        ID = np.hstack([ID,ID[sub]])

        # Cartesian atomic positions
        xyz = pos @ self.lat_real

        # projection direction of the plot
        if np.size(proj_dir) == 2:
            el = proj_dir[0]
            az = proj_dir[1]
        elif np.size(proj_dir) == 3:
            if proj_dir[0] == 0 and proj_dir[1] == 0:
                el = 90 * np.sign(proj_dir[2])
            else:
                el = np.arctan(proj_dir[2]/np.sqrt(proj_dir[0]**2 + proj_dir[1]**2)) * 180/np.pi
            az = np.arctan2(proj_dir[1],proj_dir[0]) * 180/np.pi
        else:
            raise Exception('Projection direction cannot contain ' + np.size(proj_dir) + ' elements')
        proj_dir = np.array([
            np.cos(el*np.pi/180)*np.cos(az*np.pi/180),
            np.cos(el*np.pi/180)*np.sin(az*np.pi/180),
            np.sin(el*np.pi/180),
            ])


        # 3D plotting
        fig = plt.figure(figsize=figsize)
        ax = fig.add_subplot(
            projection='3d',
            elev=el, 
            azim=az)

        # unit cell
        p = np.vstack([
            [0,0,0],
            u,
            u+v,
            v,
            w,
            u+w,
            u+v+w,
            v+w])
        p = p[:,[1,0,2]]  # Reorder cell boundaries

        f = np.array([
            [0,1,2,3],
            [4,5,6,7],
            [0,1,5,4],
            [2,3,7,6],
            [0,3,7,4],
            [1,2,6,5]]);

        # ax.plot3D(xline, yline, zline, 'gray')
        pc = art3d.Poly3DCollection(
            p[f], 
            facecolors=[0, 0.7, 1], 
            edgecolor=[0,0,0],
            linewidth=2,
            alpha=0.2,
            )
        ax.add_collection(pc)

        # # small shift of coordinates towards camera
        # d = -0.0 * proj_dir / np.linalg.norm(proj_dir)
        # print(d)

        # atoms
        ID_all = np.unique(ID)
        for ID_plot in ID_all:
            sub = ID == ID_plot
            ax.scatter(
                xs=xyz[sub,1], # + d[0], 
                ys=xyz[sub,0], # + d[1], 
                zs=xyz[sub,2], # + d[2],
                s=size_marker,
                linewidth=2,
                color=atomic_colors(ID_plot),
                edgecolor=[0,0,0])

        # plot limit
        if plot_limit is None:
            plot_limit = np.array([
                [np.min(p[:,0]), np.min(p[:,1]), np.min(p[:,2])],
                [np.max(p[:,0]), np.max(p[:,1]), np.max(p[:,2])],
                ])
            plot_limit = (plot_limit - np.mean(plot_limit,axis=0))*1.1 \
                + np.mean(plot_limit,axis=0)

        ax.axes.set_xlim3d(  left=plot_limit[0,1], right=plot_limit[1,0]) 
        ax.axes.set_ylim3d(bottom=plot_limit[0,0],   top=plot_limit[1,1]) 
        ax.axes.set_zlim3d(bottom=plot_limit[0,2],   top=plot_limit[1,2]) 
        ax.set_box_aspect((1,1,1));
        ax.invert_yaxis()
        if show_axes is False:
            ax.set_axis_off()

        plt.show();

        if returnfig:
            return fig, ax


    def plot_structure_factors(
        self,
        proj_dir:Union[list,tuple,np.ndarray]=[10,30],
        scale_markers:float=1,
        plot_limit:Optional[Union[list,tuple,np.ndarray]]=None,
        figsize:Union[list,tuple,np.ndarray]=(8,8),
        returnfig:bool=False):
        """
        3D scatter plot of the structure factors using magnitude^2, i.e. intensity.

        Args:
            dir_proj (float):           projection direction, either [elev azim] or normal vector
            scale_markers (float):      size scaling for markers
            plot_limit (float):         x y z plot limits, default is [-1 1]*self.k_max
            figsize (2 element float):  size scaling of figure axes
            returnfig (bool):           set to True to return figure and axes handles

        Returns:
            fig, ax                     (optional) figure and axes handles
        """

        if np.size(proj_dir) == 2:
            el = proj_dir[0]
            az = proj_dir[1]
        elif np.size(proj_dir) == 3:
            if proj_dir[0] == 0 and proj_dir[1] == 0:
                el = 90 * np.sign(proj_dir[2])
            else:
                el = np.arctan(proj_dir[2]/np.sqrt(proj_dir[0]**2 + proj_dir[1]**2)) * 180/np.pi
            az = np.arctan2(proj_dir[1],proj_dir[0]) * 180/np.pi
        else:
            raise Exception('Projection direction cannot contain ' + np.size(proj_dir) + ' elements')


        # 3D plotting
        fig = plt.figure(figsize=figsize)
        ax = fig.add_subplot(
            projection='3d',
            elev=el, 
            azim=az)

        ax.scatter(
            xs=self.g_vec_all[0,:], 
            ys=self.g_vec_all[1,:], 
            zs=self.g_vec_all[2,:],
            s=scale_markers*self.struct_factors_int)

        # axes limits
        if plot_limit is None:
            plot_limit = self.k_max * 1.05

        ax.axes.set_xlim3d(left=-plot_limit, right=plot_limit) 
        ax.axes.set_ylim3d(bottom=-plot_limit, top=plot_limit) 
        ax.axes.set_zlim3d(bottom=-plot_limit, top=plot_limit) 
        ax.set_box_aspect((1,1,1));
        # ax.set_axis_off()
        # ax.setxticklabels([])
        # fig.subplots_adjust(left=0, right=1, bottom=0, top=1)



        plt.show();

        if returnfig:
            return fig, ax




    def orientation_plan(
        self, 
        zone_axis_range:np.ndarray = np.array([[0,1,1],[1,1,1]]),
        angle_step_zone_axis:float = 2.0,
        angle_step_in_plane:float = 2.0,
        accel_voltage:float = 300e3, 
        corr_kernel_size:float = 0.08,
        tol_distance:float = 0.01,
        plot_corr_norm:bool = False,
        figsize:Union[list,tuple,np.ndarray] = (6,6),
        ):
        """
        Calculate the rotation basis arrays for an SO(3) rotation correlogram.
        
        Args:
            zone_axis_range (3x3 numpy float):  Row vectors give the range for zone axis orientations.
                                                Note that we always start at [0,0,1] to make z-x-z rotation work.
                                                Setting this to 'full' as a string will use a hemispherical range.
            angle_step_zone_axis (float): Approximate angular step size for zone axis [degrees]
            angle_step_in_plane (float):  Approximate angular step size for in-plane rotation [degrees]
            accel_voltage (float):        Accelerating voltage for electrons [Volts]
            corr_kernel_size (float):        Correlation kernel size length in Angstroms
            tol_distance (float):         Distance tolerance for radial shell assignment [1/Angstroms]
        """

        # Store inputs
        self.accel_voltage = np.asarray(accel_voltage)
        self.orientation_kernel_size = np.asarray(corr_kernel_size)

        # Calculate wavelenth
        self.wavelength = electron_wavelength_angstrom(self.accel_voltage)

        if isinstance(zone_axis_range, str):
            self.orientation_zone_axis_range = np.array([
                [0,0,1],
                [0,1,0],
                [1,0,0]])

            if zone_axis_range == 'full':
                self.orientation_full = True
                self.orientation_half = False
            elif zone_axis_range == 'half':
                self.orientation_full = False
                self.orientation_half = True

        else:
            # Define 3 vectors which span zone axis orientation range, normalize
            self.orientation_zone_axis_range = np.vstack((np.array([0,0,1]),np.array(zone_axis_range))).astype('float')
            self.orientation_zone_axis_range[1,:] /= np.linalg.norm(self.orientation_zone_axis_range[1,:])
            self.orientation_zone_axis_range[2,:] /= np.linalg.norm(self.orientation_zone_axis_range[2,:])

            self.orientation_full = False
            self.orientation_half = False


        # Solve for number of angular steps in zone axis (rads)
        angle_u_v = np.arccos(np.sum(self.orientation_zone_axis_range[0,:] * self.orientation_zone_axis_range[1,:]))
        angle_u_w = np.arccos(np.sum(self.orientation_zone_axis_range[0,:] * self.orientation_zone_axis_range[2,:]))
        self.orientation_zone_axis_steps = np.round(np.maximum( 
            (180/np.pi) * angle_u_v / angle_step_zone_axis,
            (180/np.pi) * angle_u_w / angle_step_zone_axis)).astype(np.int)

        # Calculate points along u and v using the SLERP formula
        # https://en.wikipedia.org/wiki/Slerp
        weights = np.linspace(0,1,self.orientation_zone_axis_steps+1)
        pv = self.orientation_zone_axis_range[0,:] * np.sin((1-weights[:,None])*angle_u_v)/np.sin(angle_u_v) + \
             self.orientation_zone_axis_range[1,:] * np.sin(   weights[:,None] *angle_u_v)/np.sin(angle_u_v) 

        # Calculate points along u and w using the SLERP formula
        pw = self.orientation_zone_axis_range[0,:] * np.sin((1-weights[:,None])*angle_u_w)/np.sin(angle_u_w) + \
             self.orientation_zone_axis_range[2,:] * np.sin(   weights[:,None] *angle_u_w)/np.sin(angle_u_w) 

        # Init array to hold all points
        self.orientation_num_zones = ((self.orientation_zone_axis_steps+1)*(self.orientation_zone_axis_steps+2)/2).astype(np.int)
        self.orientation_vecs = np.zeros((self.orientation_num_zones,3))
        self.orientation_vecs[0,:] = self.orientation_zone_axis_range[0,:]
        self.orientation_inds = np.zeros((self.orientation_num_zones,3), dtype='int')


        # Calculate zone axis points on the unit sphere with another application of SLERP
        for a0 in np.arange(1,self.orientation_zone_axis_steps+1):
            inds = np.arange(a0*(a0+1)/2, a0*(a0+1)/2 + a0 + 1).astype(np.int)

            p0 = pv[a0,:]
            p1 = pw[a0,:]
            angle_p = np.arccos(np.sum(p0 * p1))

            weights = np.linspace(0,1,a0+1)
            self.orientation_vecs[inds,:] = \
                p0[None,:] * np.sin((1-weights[:,None])*angle_p)/np.sin(angle_p) + \
                p1[None,:] * np.sin(   weights[:,None] *angle_p)/np.sin(angle_p)

            self.orientation_inds[inds,0] = a0
            self.orientation_inds[inds,1] = np.arange(a0+1)


        # expand to quarter sphere if needed
        if self.orientation_half or self.orientation_full:
            vec_new = np.copy(self.orientation_vecs) * np.array([-1,1,1])
            orientation_sector = np.zeros(vec_new.shape[0], dtype='int')

            keep = np.zeros(vec_new.shape[0],dtype='bool')
            for a0 in range(keep.size):
                if np.sqrt(np.min(np.sum((self.orientation_vecs - vec_new[a0,:])**2,axis=1))) > tol_distance:
                    keep[a0] = True

            self.orientation_vecs = np.vstack((self.orientation_vecs, vec_new[keep,:]))
            self.orientation_num_zones = self.orientation_vecs.shape[0]

            self.orientation_inds = np.vstack((
                self.orientation_inds, 
                self.orientation_inds[keep,:])).astype('int')
            self.orientation_inds[:,2] = np.hstack((
                orientation_sector,
                np.ones(np.sum(keep), dtype='int')))


        # expand to hemisphere if needed
        if self.orientation_full:
            vec_new = np.copy(self.orientation_vecs) * np.array([1,-1,1])

            keep = np.zeros(vec_new.shape[0],dtype='bool')
            for a0 in range(keep.size):
                if np.sqrt(np.min(np.sum((self.orientation_vecs - vec_new[a0,:])**2,axis=1))) > tol_distance:
                    keep[a0] = True

            self.orientation_vecs = np.vstack((self.orientation_vecs, vec_new[keep,:]))
            self.orientation_num_zones = self.orientation_vecs.shape[0]

            orientation_sector = np.hstack((
                self.orientation_inds[:,2],
                self.orientation_inds[keep,2] + 2))
            self.orientation_inds = np.vstack((
                self.orientation_inds, 
                self.orientation_inds[keep,:])).astype('int')
            self.orientation_inds[:,2] = orientation_sector


        # Convert to spherical coordinates
        elev = np.arctan2(np.hypot(
            self.orientation_vecs[:,0], 
            self.orientation_vecs[:,1]), 
            self.orientation_vecs[:,2])
        azim = -np.pi/2 + np.arctan2(
            self.orientation_vecs[:,1],
            self.orientation_vecs[:,0])


        # Solve for number of angular steps along in-plane rotation direction
        self.orientation_in_plane_steps = np.round(360/angle_step_in_plane).astype(np.int)

        # Calculate -z angles (Euler angle 3)
        self.orientation_gamma = np.linspace(0,2*np.pi,self.orientation_in_plane_steps, endpoint=False)

        # Determine the radii of all spherical shells
        radii_test = np.round(self.g_vec_leng / tol_distance) * tol_distance
        radii = np.unique(radii_test)
        # Remove zero beam
        keep = np.abs(radii) > tol_distance 
        self.orientation_shell_radii = radii[keep]

        # init
        self.orientation_shell_index = -1*np.ones(self.g_vec_all.shape[1], dtype='int')
        self.orientation_shell_count = np.zeros(self.orientation_shell_radii.size)

        # Assign each structure factor point to a radial shell
        for a0 in range(self.orientation_shell_radii.size):
            sub = np.abs(self.orientation_shell_radii[a0] - radii_test) <= tol_distance / 2

            self.orientation_shell_index[sub] = a0
            self.orientation_shell_count[a0] = np.sum(sub)
            self.orientation_shell_radii[a0] = np.mean(self.g_vec_leng[sub])

        # init storage arrays
        self.orientation_rotation_angles = np.zeros((self.orientation_num_zones,2))
        self.orientation_rotation_matrices = np.zeros((self.orientation_num_zones,3,3))
        self.orientation_ref = np.zeros((
            self.orientation_num_zones,
            np.size(self.orientation_shell_radii),
            self.orientation_in_plane_steps),
            dtype='complex64')

        # Calculate rotation matrices for zone axes
        # for a0 in tqdmnd(np.arange(self.orientation_num_zones),desc='Computing orientation basis',unit=' terms',unit_scale=True):
        for a0 in np.arange(self.orientation_num_zones):
            m1z = np.array([
                [ np.cos(azim[a0]), -np.sin(azim[a0]), 0],
                [ np.sin(azim[a0]),  np.cos(azim[a0]), 0],
                [ 0,                 0,                1]])
            m2x = np.array([
                [1,  0,                0],
                [0,  np.cos(elev[a0]), np.sin(elev[a0])],
                [0, -np.sin(elev[a0]),  np.cos(elev[a0])]])
            self.orientation_rotation_matrices[a0,:,:] = m1z @ m2x
            self.orientation_rotation_angles[a0,:] = [azim[a0], elev[a0]]

        # init
        k0 = np.array([0, 0, 1]) / self.wavelength
        dphi = self.orientation_gamma[1] - self.orientation_gamma[0]

        # Calculate reference arrays for all orientations
        for a0 in tqdmnd(np.arange(self.orientation_num_zones), desc="Orientation plan", unit=" zone axes"):
            p = np.linalg.inv(self.orientation_rotation_matrices[a0,:,:]) @ self.g_vec_all

            # Excitation errors
            cos_alpha = (k0[2,None] + p[2,:]) \
                / np.linalg.norm(k0[:,None] + p, axis=0)
            sg = (-0.5) * np.sum((2*k0[:,None] + p) * p, axis=0) \
                / (np.linalg.norm(k0[:,None] + p, axis=0)) / cos_alpha

            # in-plane rotation angle
            phi = np.arctan2(p[1,:],p[0,:])

            for a1 in np.arange(self.g_vec_all.shape[1]):
                ind_radial = self.orientation_shell_index[a1]

                if ind_radial >= 0:
                    self.orientation_ref[a0,ind_radial,:] += \
                        self.orientation_shell_radii[ind_radial] * np.sqrt(self.struct_factors_int[a1]) * \
                        np.maximum(1 - np.sqrt(sg[a1]**2 + \
                        ((np.mod(self.orientation_gamma - phi[a1] + np.pi, 2*np.pi) - np.pi) * \
                        self.orientation_shell_radii[ind_radial])**2) / self.orientation_kernel_size, 0)

            # Normalization
            self.orientation_ref[a0,:,:] /= \
                np.sqrt(np.sum(self.orientation_ref[a0,:,:]**2))

        # Maximum value
        self.orientation_ref_max = np.max(np.real(self.orientation_ref))

        # Fourier domain along angular axis
        # self.orientation_ref = np.fft.fft(self.orientation_ref)
        self.orientation_ref = np.conj(np.fft.fft(self.orientation_ref))


        # plot the correlation normalization
        if plot_corr_norm is True:
            # 2D correlation slice
            im_corr_zone_axis = np.zeros((self.orientation_zone_axis_steps+1, self.orientation_zone_axis_steps+1))
            for a0 in np.arange(self.orientation_zone_axis_steps+1):
                inds_val = np.arange(a0*(a0+1)/2, a0*(a0+1)/2 + a0 + 1).astype(np.int)
                im_corr_zone_axis[a0,range(a0+1)] = self.orientation_corr_norm[inds_val]

            # Zone axis
            fig, ax = plt.subplots(figsize=figsize)
            # cmin = np.min(self.orientation_corr_norm)
            cmax = np.max(self.orientation_corr_norm)

            # im_plot = (im_corr_zone_axis - cmin) / (cmax - cmin)
            im_plot = im_corr_zone_axis / cmax 

            im = ax.imshow(
                im_plot,
                cmap='viridis',
                vmin=0.0,
                vmax=1.0)
            fig.colorbar(im)

            label_0 = self.orientation_zone_axis_range[0,:]
            label_0 = np.round(label_0 * 1e3) * 1e-3
            label_0 /= np.min(np.abs(label_0[np.abs(label_0)>0]))

            label_1 = self.orientation_zone_axis_range[1,:]
            label_1 = np.round(label_1 * 1e3) * 1e-3
            label_1 /= np.min(np.abs(label_1[np.abs(label_1)>0]))

            label_2 = self.orientation_zone_axis_range[2,:]
            label_2 = np.round(label_2 * 1e3) * 1e-3
            label_2 /= np.min(np.abs(label_2[np.abs(label_2)>0]))

            ax.set_yticks([0])
            ax.set_yticklabels([
                str(label_0)])

            ax.set_xticks([0, self.orientation_zone_axis_steps])
            ax.set_xticklabels([
                str(label_1),
                str(label_2)])

            plt.show()  


    def plot_orientation_zones(
        self,
        proj_dir:Optional[Union[list,tuple,np.ndarray]]=None,
        marker_size:float=20,
        plot_limit:Union[list,tuple,np.ndarray]=np.array([-1.1, 1.1]),
        figsize:Union[list,tuple,np.ndarray]=(8,8),
        returnfig:bool=False):
        """
        3D scatter plot of the structure factors using magnitude^2, i.e. intensity.

        Args:
            dir_proj (float):           projection direction, either [elev azim] or normal vector
                                        Default is mean vector of self.orientation_zone_axis_range rows
            marker_size (float):        size of markers
            plot_limit (float):         x y z plot limits, default is [0, 1.05]
            figsize (2 element float):  size scaling of figure axes
            returnfig (bool):           set to True to return figure and axes handles

        Returns:
            fig, ax                     (optional) figure and axes handles
        """

        if proj_dir is None:
            proj_dir = np.mean(self.orientation_zone_axis_range, axis=0)

        if np.size(proj_dir) == 2:
            el = proj_dir[0]
            az = proj_dir[1]
        elif np.size(proj_dir) == 3:
            if proj_dir[0] == 0 and proj_dir[1] == 0:
                el = 90 * np.sign(proj_dir[2])
            else:
                el = np.arctan(proj_dir[2]/np.sqrt(proj_dir[0]**2 + proj_dir[1]**2)) * 180/np.pi
            az = np.arctan2(proj_dir[1],proj_dir[0]) * 180/np.pi
        else:
            raise Exception('Projection direction cannot contain ' + np.size(proj_dir) + ' elements')


        # 3D plotting
        fig = plt.figure(figsize=figsize)
        ax = fig.add_subplot(
            projection='3d',
            elev=el, 
            azim=90-az)


        # Sphere
        # Make data
        u = np.linspace(0, 2 * np.pi, 100)
        v = np.linspace(0, np.pi, 100)
        r = 0.95
        x = r * np.outer(np.cos(u), np.sin(v))
        y = r * np.outer(np.sin(u), np.sin(v))
        z = r * np.outer(np.ones(np.size(u)), np.cos(v))
        # Plot the surface
        ax.plot_surface(
            x, 
            y, 
            z,
            edgecolor=None,
            color=np.array([1.0,0.8,0.0]),
            alpha=0.4,
            antialiased=True,
            )

        # Lines
        r = 0.951
        t = np.linspace(0, 2 * np.pi, 181)
        t0 = np.zeros((181,))
        # z = np.linspace(-2, 2, 100)
        # r = z**2 + 1
        # x = r * np.sin(theta)
        # y = r * np.cos(theta)

        warnings.filterwarnings( "ignore", module = "matplotlib\..*" )
        line_params = {
            'linewidth': 2,
            'alpha': 0.1,
            'c': 'k'}
        for phi in np.arange(0,180,5):
            ax.plot3D(
                np.sin(phi*np.pi/180)*np.cos(t)*r, 
                np.sin(phi*np.pi/180)*np.sin(t)*r, 
                np.cos(phi*np.pi/180)*r, **line_params)

        # plot zone axes
        ax.scatter(
            xs=self.orientation_vecs[:,1], 
            ys=self.orientation_vecs[:,0], 
            zs=self.orientation_vecs[:,2],
            s=marker_size)

        # zone axis range labels
        label_0 = self.orientation_zone_axis_range[0,:]
        label_0 = np.round(label_0 * 1e3) * 1e-3
        label_0 /= np.min(np.abs(label_0[np.abs(label_0)>0]))

        label_1 = self.orientation_zone_axis_range[1,:]
        label_1 = np.round(label_1 * 1e3) * 1e-3
        label_1 /= np.min(np.abs(label_1[np.abs(label_1)>0]))

        label_2 = self.orientation_zone_axis_range[2,:]
        label_2 = np.round(label_2 * 1e3) * 1e-3
        label_2 /= np.min(np.abs(label_2[np.abs(label_2)>0]))

        inds = np.array([0,
            self.orientation_num_zones - self.orientation_zone_axis_steps - 1,
            self.orientation_num_zones - 1])

        ax.scatter(
            xs=self.orientation_vecs[inds,1]*1.02, 
            ys=self.orientation_vecs[inds,0]*1.02, 
            zs=self.orientation_vecs[inds,2]*1.02,
            s=marker_size*8,
            linewidth=2,
            marker='o',
            edgecolors='r',
            alpha=1,
            zorder=10)

        text_scale_pos = 1.2
        text_params = {
            'va': 'center',
            'family': 'sans-serif',
            'fontweight': 'normal',
            'color': 'k',
            'size': 20}
        # 'ha': 'center',

        ax.text(
            self.orientation_vecs[inds[0],1]*text_scale_pos, 
            self.orientation_vecs[inds[0],0]*text_scale_pos, 
            self.orientation_vecs[inds[0],2]*text_scale_pos, 
            label_0, 
            None,
            zorder=11,
            ha='center',
            **text_params)
        ax.text(
            self.orientation_vecs[inds[1],1]*text_scale_pos, 
            self.orientation_vecs[inds[1],0]*text_scale_pos, 
            self.orientation_vecs[inds[1],2]*text_scale_pos, 
            label_1, 
            None,
            zorder=12,
            ha='right',
            **text_params)
        ax.text(
            self.orientation_vecs[inds[2],1]*text_scale_pos, 
            self.orientation_vecs[inds[2],0]*text_scale_pos, 
            self.orientation_vecs[inds[2],2]*text_scale_pos, 
            label_2, 
            None,
            zorder=13,
            ha='left',
            **text_params)

        # ax.scatter(
        #     xs=self.g_vec_all[0,:], 
        #     ys=self.g_vec_all[1,:], 
        #     zs=self.g_vec_all[2,:],
        #     s=scale_markers*self.struct_factors_int)

        # axes limits
        ax.axes.set_xlim3d(left=plot_limit[0], right=plot_limit[1]) 
        ax.axes.set_ylim3d(bottom=plot_limit[0], top=plot_limit[1]) 
        ax.axes.set_zlim3d(bottom=plot_limit[0], top=plot_limit[1]) 
        ax.set_box_aspect((1,1,1));
        ax.set_axis_off()
        # ax.setxticklabels([])
        # fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
        # plt.gca().invert_yaxis()
        ax.view_init(elev=el, azim=90-az)

        plt.show();

        if returnfig:
            return fig, ax


    def plot_orientation_plan(
        self,
        index_plot:int=0,
        figsize:Union[list,tuple,np.ndarray]=(14,6),
        returnfig:bool=False
        ):
        """
        3D scatter plot of the structure factors using magnitude^2, i.e. intensity.

        Args:
            index_plot (int):           which zone axis slice to plot
            figsize (2 element float):  size scaling of figure axes
            returnfig (bool):           set to True to return figure and axes handles

        Returns:
            fig, ax                     (optional) figure and axes handles
        """

        fig, ax = plt.subplots(1,2,figsize=figsize)

        # Generate and plot diffraction pattern
        k_x_y_range = np.array([1,1])*self.k_max*1.2
        bragg_peaks = self.generate_diffraction_pattern(
            zone_axis = self.orientation_vecs[index_plot,:],
            sigma_excitation_error=self.orientation_kernel_size/3)
        plot_diffraction_pattern(
            bragg_peaks,
            figsize=(figsize[1],figsize[1]),
            plot_range_kx_ky=k_x_y_range,
            scale_markers=10,
            shift_labels=0.10,
            input_fig_handle=[fig, ax])

        # Plot orientation plan
        im_plot = np.real(np.fft.ifft(self.orientation_ref[index_plot,:,:],axis=1)).astype('float')
        im_plot = im_plot / self.orientation_ref_max

        # coordinates
        x = self.orientation_gamma * 180 / np.pi
        y = np.arange(np.size(self.orientation_shell_radii))
        dx = (x[1]-x[0])/2.
        dy = (y[1]-y[0])/2.
        extent = [x[0]-dx, x[-1]+dx, y[-1]+dy, y[0]-dy]

        im = ax[1].imshow(
            im_plot,
            cmap='inferno',
            vmin=0.0,
            vmax=0.5,
            extent=extent,
            aspect='auto',
            interpolation='none')
        fig.colorbar(im)
        ax[1].xaxis.tick_top()
        ax[1].set_xticks(np.arange(0,360+90,90))
        ax[1].set_ylabel(
            'Radial Index',
            size=20)

        # Add text label 
        zone_axis_fit = self.orientation_vecs[index_plot,:]
        zone_axis_fit = zone_axis_fit / np.linalg.norm(zone_axis_fit)
        sub = np.abs(zone_axis_fit) > 0
        scale = np.min(np.abs(zone_axis_fit[sub]))
        if scale > 0.14:
            zone_axis_fit = zone_axis_fit \
                / scale

        temp = np.round(zone_axis_fit * 1e2) / 1e2
        ax[0].text( \
            -k_x_y_range[0]*0.95,
            -k_x_y_range[1]*0.95,
            '[' + 
            str(temp[0]) + ', ' +
            str(temp[1]) + ', ' +
            str(temp[2]) + ']',
            size=18,
            va='top') 

        # plt.tight_layout()
        plt.show()

        if returnfig:
            return fig, ax


    def match_orientations(
       self,
       bragg_peaks_array:PointListArray,
       num_matches_return:int = 1,
       return_corr:bool=False,
       subpixel_tilt:bool=False,
       ):

        if num_matches_return == 1:
            orientation_matrices = np.zeros((*bragg_peaks_array.shape, 3, 3),dtype=np.float64)
            if return_corr:
                corr_all = np.zeros(bragg_peaks_array.shape,dtype=np.float64)
        else:
            orientation_matrices = np.zeros((*bragg_peaks_array.shape, 3, 3, num_matches_return),dtype=np.float64)
            if return_corr:
                corr_all = np.zeros((*bragg_peaks_array.shape, num_matches_return),dtype=np.float64)

        for rx,ry in tqdmnd(*bragg_peaks_array.shape, desc="Matching Orientations", unit=" PointList"):
            bragg_peaks = bragg_peaks_array.get_pointlist(rx,ry)

            if return_corr:
                orientation_matrices[rx,ry], corr_all[rx,ry] = self.match_single_pattern(
                    bragg_peaks,
                    subpixel_tilt=subpixel_tilt,
                    num_matches_return=num_matches_return,
                    plot_corr=False,
                    plot_corr_3D=False,
                    return_corr=True,
                    verbose=False,
                    )
            else:
                orientation_matrices[rx,ry] = self.match_single_pattern(
                    bragg_peaks,
                    subpixel_tilt=subpixel_tilt,
                    num_matches_return=num_matches_return,
                    plot_corr=False,
                    plot_corr_3D=False,
                    return_corr=False,
                    verbose=False,
                    )

        if return_corr:
            return orientation_matrices, corr_all
        else:
            return orientation_matrices

    def match_single_pattern(
        self,
        bragg_peaks:PointList,
        num_matches_return:int = 1,
        tol_peak_delete:Optional[float] = None,
        subpixel_tilt:bool=False,
        plot_corr:bool=False,
        plot_corr_3D:bool=False,
        return_corr:bool=False,
        returnfig:bool=False,
        figsize:Union[list,tuple,np.ndarray]=(12,4),
        verbose:bool=False,
        ):
        """
        Solve for the best fit orientation of a single diffraction pattern.

        Args:
            bragg_peaks (PointList):            numpy array containing the Bragg positions and intensities ('qx', 'qy', 'intensity')
            num_matches_return (int):           return these many matches as 3th dim of orient (matrix)
            tol_peak_delete (float):            Distance to delete peaks for multiple matches.
                                                Default is kernel_size * 0.5
            subpixel_tilt (bool):               set to false for faster matching, returning the nearest corr point
            plot_corr (bool):                   set to true to plot the resulting correlogram

        Returns:
            orientation_output (3x3xN float)    orienation matrix where zone axis is the 3rd column, 3rd dim for multiple matches
            corr_value (float):                 (optional) return correlation values
        """

        # get bragg peak data
        qx = bragg_peaks.data['qx']
        qy = bragg_peaks.data['qy']
        intensity = bragg_peaks.data['intensity']


        # init orientation output, delete distance threshold squared
        if num_matches_return == 1:
            orientation_output = np.zeros((3,3))
        else:
            orientation_output = np.zeros((3,3,num_matches_return))

            if tol_peak_delete is None:
                tol_peak_delete = self.orientation_kernel_size * 0.5

            # r_del_2 = tol_peak_delete**2
            corr_output = np.zeros((num_matches_return))

        # loop over the number of matches to return
        for match_ind in range(num_matches_return):
            # Convert Bragg peaks to polar coordinates
            qr = np.sqrt(qx**2 + qy**2)
            qphi = np.arctan2(qy, qx)

            # Calculate polar Bragg peak image
            im_polar = np.zeros((
                np.size(self.orientation_shell_radii),
                self.orientation_in_plane_steps),
                dtype='float')

            for ind_radial, radius in enumerate(self.orientation_shell_radii):
                dqr = np.abs(qr - radius)
                sub = dqr < self.orientation_kernel_size

                if np.sum(sub) > 0:
                    im_polar[ind_radial,:] = np.sum(
                        radius * np.sqrt(intensity[sub,None]) 
                        * np.maximum(1 - np.sqrt(dqr[sub,None]**2 + \
                        ((np.mod(self.orientation_gamma[None,:] - qphi[sub,None] + np.pi, 2*np.pi) - np.pi) * \
                        radius)**2) / self.orientation_kernel_size, 0), axis=0)

            # Calculate orientation correlogram
            corr_full = np.sum(np.real(np.fft.ifft(self.orientation_ref * np.fft.fft(im_polar[None,:,:]))), axis=1)
            # Find best match for each zone axis
            ind_phi = np.argmax(corr_full, axis=1)
            # print(self.orientation_gamma*180./np.pi)
            corr_value = np.zeros(self.orientation_num_zones)
            corr_in_plane_angle = np.zeros(self.orientation_num_zones)
            dphi = self.orientation_gamma[1] - self.orientation_gamma[0]

            for a0 in range(self.orientation_num_zones):
                inds = np.mod(ind_phi[a0] + np.arange(-1,2), self.orientation_gamma.size).astype('int')
                c = corr_full[a0,inds]

                if np.max(c) > 0:
                    corr_value[a0] = c[1] + (c[0]-c[2])**2 / (4*(2*c[1]-c[0]-c[2])**2)
                    dc = (c[2]-c[0]) / (4*c[1] - 2*c[0] - 2*c[2])
                    corr_in_plane_angle[a0] = self.orientation_gamma[ind_phi[a0]] + dc*dphi

            # Determine the best fit orientation
            ind_best_fit = np.unravel_index(np.argmax(corr_value), corr_value.shape)[0]

            # Get orientation matrix
            if subpixel_tilt is False:
                orientation_matrix = np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])

            else:
                def ind_to_sub(ind):
                    ind_x = np.floor(0.5*np.sqrt(8.0*ind+1) - 0.5).astype('int')
                    ind_y = ind - np.floor(ind_x*(ind_x+1)/2).astype('int')
                    return ind_x, ind_y
                def sub_to_ind(ind_x, ind_y):
                    return (np.floor(ind_x*(ind_x+1)/2) + ind_y).astype('int')

                # Sub pixel refinement of zone axis orientation
                if ind_best_fit == 0:
                    # Zone axis is (0,0,1)
                    orientation_matrix = np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])

                elif ind_best_fit == self.orientation_num_zones - self.orientation_zone_axis_steps - 1:
                    # Zone axis is 1st user provided direction
                    orientation_matrix = np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])

                elif ind_best_fit == self.orientation_num_zones - 1:
                    # Zone axis is the 2nd user-provided direction
                    orientation_matrix = np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])

                else:
                    ind_x, ind_y = ind_to_sub(ind_best_fit)
                    max_x, max_y = ind_to_sub(self.orientation_num_zones-1)

                    if ind_y == 0:
                        ind_x_prev = sub_to_ind(ind_x-1, 0)
                        ind_x_post = sub_to_ind(ind_x+1, 0)

                        c = np.array([corr_value[ind_x_prev], corr_value[ind_best_fit], corr_value[ind_x_post]])
                        dc = (c[2]-c[0]) / (4*c[1] - 2*c[0] - 2*c[2])

                        if dc > 0:
                            orientation_matrix = \
                                np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])*(1-dc) + \
                                np.squeeze(self.orientation_rotation_matrices[ind_x_post,:,:])*dc
                        else:
                            orientation_matrix = \
                                np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])*(1+dc) + \
                                np.squeeze(self.orientation_rotation_matrices[ind_x_prev,:,:])*-dc

                    elif ind_x == max_x:
                        ind_x_prev = sub_to_ind(max_x, ind_y-1)
                        ind_x_post = sub_to_ind(max_x, ind_y+1)

                        c = np.array([corr_value[ind_x_prev], corr_value[ind_best_fit], corr_value[ind_x_post]])
                        dc = (c[2]-c[0]) / (4*c[1] - 2*c[0] - 2*c[2])
                        
                        if dc > 0:
                            orientation_matrix = \
                                np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])*(1-dc) + \
                                np.squeeze(self.orientation_rotation_matrices[ind_x_post,:,:])*dc
                        else:
                            orientation_matrix = \
                                np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])*(1+dc) + \
                                np.squeeze(self.orientation_rotation_matrices[ind_x_prev,:,:])*-dc


                    elif ind_x == ind_y:
                        ind_x_prev = sub_to_ind(ind_x-1, ind_y-1)
                        ind_x_post = sub_to_ind(ind_x+1, ind_y+1)

                        c = np.array([corr_value[ind_x_prev], corr_value[ind_best_fit], corr_value[ind_x_post]])
                        dc = (c[2]-c[0]) / (4*c[1] - 2*c[0] - 2*c[2])

                        if dc > 0:
                            orientation_matrix = \
                                np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])*(1-dc) + \
                                np.squeeze(self.orientation_rotation_matrices[ind_x_post,:,:])*dc
                        else:
                            orientation_matrix = \
                                np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])*(1+dc) + \
                                np.squeeze(self.orientation_rotation_matrices[ind_x_prev,:,:])*-dc

                    else:
                        # # best fit point is not on any of the corners or edges
                        ind_1 = sub_to_ind(ind_x-1, ind_y-1)
                        ind_2 = sub_to_ind(ind_x-1, ind_y  )
                        ind_3 = sub_to_ind(ind_x  , ind_y-1)
                        ind_4 = sub_to_ind(ind_x  , ind_y+1)
                        ind_5 = sub_to_ind(ind_x+1, ind_y  )
                        ind_6 = sub_to_ind(ind_x+1, ind_y+1)

                        c = np.array([ \
                            (corr_value[ind_1]+corr_value[ind_2])/2, 
                            corr_value[ind_best_fit], 
                            (corr_value[ind_5]+corr_value[ind_6])/2])
                        dx = (c[2]-c[0]) / (4*c[1] - 2*c[0] - 2*c[2])

                        c = np.array([corr_value[ind_3], corr_value[ind_best_fit], corr_value[ind_4]])
                        dy = (c[2]-c[0]) / (4*c[1] - 2*c[0] - 2*c[2])

                        if dx > 0:
                            if dy > 0:
                                orientation_matrix = \
                                    np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])*(1-dx)*(1-dy) + \
                                    np.squeeze(self.orientation_rotation_matrices[ind_4,:,:])       *(1-dx)*(  dy) + \
                                    np.squeeze(self.orientation_rotation_matrices[ind_6,:,:])       *dx
                            else:
                                orientation_matrix = \
                                    np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])*(1-dx)*(1+dy) + \
                                    np.squeeze(self.orientation_rotation_matrices[ind_3,:,:])       *(1-dx)*( -dy) + \
                                    np.squeeze(self.orientation_rotation_matrices[ind_5,:,:])       *dx
                        else:
                            if dy > 0:
                                orientation_matrix = \
                                    np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])*(1+dx)*(1-dy) + \
                                    np.squeeze(self.orientation_rotation_matrices[ind_4,:,:])       *(1+dx)*(  dy) + \
                                    np.squeeze(self.orientation_rotation_matrices[ind_2,:,:])       *-dx
                            else:
                                orientation_matrix = \
                                    np.squeeze(self.orientation_rotation_matrices[ind_best_fit,:,:])*(1+dx)*(1+dy) + \
                                    np.squeeze(self.orientation_rotation_matrices[ind_3,:,:])       *(1+dx)*( -dy) + \
                                    np.squeeze(self.orientation_rotation_matrices[ind_1,:,:])       *-dx

            # apply in-plane rotation
            phi = corr_in_plane_angle[ind_best_fit] # + np.pi
            m3z = np.array([
                    [ np.cos(phi), np.sin(phi), 0],
                    [-np.sin(phi), np.cos(phi), 0],
                    [ 0,           0,           1]])
            orientation_matrix = orientation_matrix @ m3z

            # Output the orientation matrix
            if num_matches_return == 1:
                orientation_output = orientation_matrix
                corr_output = corr_value[ind_best_fit]

            else:
                orientation_output[:,:,match_ind] = orientation_matrix
                corr_output[match_ind] = corr_value[ind_best_fit]

            if verbose:
                zone_axis_fit = orientation_matrix[:,2]
                temp = zone_axis_fit / np.linalg.norm(zone_axis_fit)
                temp = np.round(temp * 1e3) / 1e3
                print('Best fit zone axis = (' 
                    + str(temp) + ')' 
                    + ' with corr value = ' 
                    + str(np.round(corr_value[ind_best_fit] * 1e3) / 1e3))

            # if needed, delete peaks for next iteration
            if num_matches_return > 1:
                bragg_peaks_fit = self.generate_diffraction_pattern(
                    orientation_matrix,
                    sigma_excitation_error=self.orientation_kernel_size)

                remove = np.zeros_like(qx,dtype='bool')
                scale_int = np.zeros_like(qx)
                for a0 in np.arange(qx.size):
                    d_2 = (bragg_peaks_fit.data['qx'] - qx[a0])**2 \
                        + (bragg_peaks_fit.data['qy'] - qy[a0])**2

                    dist_min = np.sqrt(np.min(d_2))

                    if dist_min < tol_peak_delete:
                        remove[a0] = True
                    elif dist_min < self.orientation_kernel_size:
                        scale_int[a0] = (dist_min - tol_peak_delete) \
                        / (self.orientation_kernel_size - tol_peak_delete)

                intensity = intensity * scale_int
                qx = qx[~remove]
                qy = qy[~remove]
                intensity = intensity[~remove]


        # plotting correlation image
        if plot_corr is True:


            if self.orientation_full:
                fig, ax = plt.subplots(1, 2, figsize=figsize*np.array([2,2]))
                cmin = np.min(corr_value)
                cmax = np.max(corr_value)

                im_corr_zone_axis = np.zeros((
                    2*self.orientation_zone_axis_steps+1, 
                    2*self.orientation_zone_axis_steps+1))
                
                sub = self.orientation_inds[:,2] == 0
                x_inds = (self.orientation_inds[sub,0] - self.orientation_inds[sub,1]).astype('int') \
                    + self.orientation_zone_axis_steps
                y_inds = self.orientation_inds[sub,1].astype('int') \
                    + self.orientation_zone_axis_steps
                inds_1D = np.ravel_multi_index([x_inds, y_inds], im_corr_zone_axis.shape)
                im_corr_zone_axis.ravel()[inds_1D] = corr_value[sub]

                sub = self.orientation_inds[:,2] == 1
                x_inds = (self.orientation_inds[sub,0] - self.orientation_inds[sub,1]).astype('int') \
                    + self.orientation_zone_axis_steps
                y_inds = self.orientation_zone_axis_steps - self.orientation_inds[sub,1].astype('int') 
                inds_1D = np.ravel_multi_index([x_inds, y_inds], im_corr_zone_axis.shape)
                im_corr_zone_axis.ravel()[inds_1D] = corr_value[sub]

                sub = self.orientation_inds[:,2] == 2
                x_inds = (self.orientation_inds[sub,1] - self.orientation_inds[sub,0]).astype('int') \
                    + self.orientation_zone_axis_steps
                y_inds = self.orientation_inds[sub,1].astype('int') \
                    + self.orientation_zone_axis_steps
                inds_1D = np.ravel_multi_index([x_inds, y_inds], im_corr_zone_axis.shape)
                im_corr_zone_axis.ravel()[inds_1D] = corr_value[sub]

                sub = self.orientation_inds[:,2] == 3
                x_inds = (self.orientation_inds[sub,1] - self.orientation_inds[sub,0]).astype('int') \
                    + self.orientation_zone_axis_steps
                y_inds = self.orientation_zone_axis_steps - self.orientation_inds[sub,1].astype('int') 
                inds_1D = np.ravel_multi_index([x_inds, y_inds], im_corr_zone_axis.shape)
                im_corr_zone_axis.ravel()[inds_1D] = corr_value[sub]

                im_plot = (im_corr_zone_axis - cmin) / (cmax - cmin)
                ax[0].imshow(
                    im_plot,
                    cmap='viridis',
                    vmin=0.0,
                    vmax=1.0)

            elif self.orientation_half:
                fig, ax = plt.subplots(1, 2, figsize=figsize*np.array([2,1]))
                cmin = np.min(corr_value)
                cmax = np.max(corr_value)

                im_corr_zone_axis = np.zeros((
                    self.orientation_zone_axis_steps+1, 
                    self.orientation_zone_axis_steps*2+1))
                
                sub = self.orientation_inds[:,2] == 0
                x_inds = (self.orientation_inds[sub,0] - self.orientation_inds[sub,1]).astype('int')
                y_inds = self.orientation_inds[sub,1].astype('int') + self.orientation_zone_axis_steps
                inds_1D = np.ravel_multi_index([x_inds, y_inds], im_corr_zone_axis.shape)
                im_corr_zone_axis.ravel()[inds_1D] = corr_value[sub]

                sub = self.orientation_inds[:,2] == 1
                x_inds = (self.orientation_inds[sub,0] - self.orientation_inds[sub,1]).astype('int')
                y_inds = self.orientation_zone_axis_steps - self.orientation_inds[sub,1].astype('int') 
                inds_1D = np.ravel_multi_index([x_inds, y_inds], im_corr_zone_axis.shape)
                im_corr_zone_axis.ravel()[inds_1D] = corr_value[sub]

                im_plot = (im_corr_zone_axis - cmin) / (cmax - cmin)
                ax[0].imshow(
                    im_plot,
                    cmap='viridis',
                    vmin=0.0,
                    vmax=1.0)


            else:
                fig, ax = plt.subplots(1, 2, figsize=figsize)
                cmin = np.min(corr_value)
                cmax = np.max(corr_value)

                im_corr_zone_axis = np.zeros((self.orientation_zone_axis_steps+1, self.orientation_zone_axis_steps+1))
                im_mask = np.ones((
                    self.orientation_zone_axis_steps+1, 
                    self.orientation_zone_axis_steps+1),
                    dtype='bool')


                x_inds = (self.orientation_inds[:,0] - self.orientation_inds[:,1]).astype('int')
                y_inds = self.orientation_inds[:,1].astype('int')
                inds_1D = np.ravel_multi_index([x_inds, y_inds], im_corr_zone_axis.shape)
                im_corr_zone_axis.ravel()[inds_1D] = corr_value
                im_mask.ravel()[inds_1D] = False

                im_plot = np.ma.masked_array(
                     (im_corr_zone_axis - cmin) / (cmax - cmin),
                     mask=im_mask)

                ax[0].imshow(
                    im_plot,
                    cmap='viridis',
                    vmin=0.0,
                    vmax=1.0)
                ax[0].spines['left'].set_color('none')
                ax[0].spines['right'].set_color('none')
                ax[0].spines['top'].set_color('none')
                ax[0].spines['bottom'].set_color('none')


                inds_plot = np.unravel_index(np.argmax(im_plot, axis=None), im_plot.shape)
                ax[0].scatter(inds_plot[1],inds_plot[0], s=120, linewidth = 2, facecolors='none', edgecolors='r')

                label_0 = self.orientation_zone_axis_range[0,:]
                label_0 = np.round(label_0 * 1e3) * 1e-3
                label_0 /= np.min(np.abs(label_0[np.abs(label_0)>0]))

                label_1 = self.orientation_zone_axis_range[1,:]
                label_1 = np.round(label_1 * 1e3) * 1e-3
                label_1 /= np.min(np.abs(label_1[np.abs(label_1)>0]))

                label_2 = self.orientation_zone_axis_range[2,:]
                label_2 = np.round(label_2 * 1e3) * 1e-3
                label_2 /= np.min(np.abs(label_2[np.abs(label_2)>0]))

                ax[0].set_xticks([0, self.orientation_zone_axis_steps])
                ax[0].set_xticklabels([
                    str(label_0),
                    str(label_2)],
                    size=14)
                ax[0].xaxis.tick_top()

                ax[0].set_yticks([self.orientation_zone_axis_steps])
                ax[0].set_yticklabels([
                    str(label_1)],
                    size=14)

            # In-plane rotation
            ax[1].plot(
                self.orientation_gamma * 180/np.pi, 
                (np.squeeze(corr_full[ind_best_fit,:]) - cmin)/(cmax - cmin));
            ax[1].set_xlabel('In-plane rotation angle [deg]',
                size=16)
            ax[1].set_ylabel('Corr. of Best Fit Zone Axis',
                size=16)
            ax[1].set_ylim([0,1.01])

            plt.show()

        if plot_corr_3D is True:
                    # 3D plotting

            fig = plt.figure(figsize=[figsize[0],figsize[0]])
            ax = fig.add_subplot(
                projection='3d',
                elev=90, 
                azim=0)

            sig_zone_axis = np.max(corr,axis=1)

            el = self.orientation_rotation_angles[:,0,0]
            az = self.orientation_rotation_angles[:,0,1]
            x = np.cos(az)*np.sin(el)
            y = np.sin(az)*np.sin(el)
            z =            np.cos(el)

            v = np.vstack((x.ravel(),y.ravel(),z.ravel()))

            v_order = np.array([
                [0,1,2],
                [0,2,1],
                [1,0,2],
                [1,2,0],
                [2,0,1],
                [2,1,0],
                ])
            d_sign = np.array([
                [ 1, 1, 1],
                [-1, 1, 1],
                [ 1,-1, 1],
                [-1,-1, 1],
                ])

            for a1 in range(d_sign.shape[0]):
                for a0 in range(v_order.shape[0]):
                    ax.scatter(
                        xs=v[v_order[a0,0]] * d_sign[a1,0], 
                        ys=v[v_order[a0,1]] * d_sign[a1,1], 
                        zs=v[v_order[a0,2]] * d_sign[a1,2],
                        s=30,
                        c=sig_zone_axis.ravel(),
                        edgecolors=None)


            # axes limits
            r = 1.05
            ax.axes.set_xlim3d(left=-r, right=r) 
            ax.axes.set_ylim3d(bottom=-r, top=r) 
            ax.axes.set_zlim3d(bottom=-r, top=r) 
            axisEqual3D(ax)


            plt.show()


        if return_corr:
            if returnfig:
                return orientation_output, corr_output, fig, ax
            else:
                return orientation_output, corr_output
        else:
            if returnfig:
                return orientation_output, fig, ax
            else:
                return orientation_output


    def generate_diffraction_pattern(
        self, 
        zone_axis:Union[list,tuple,np.ndarray] = [0,0,1],
        foil_normal:Optional[Union[list,tuple,np.ndarray]] = None,
        proj_x_axis:Optional[Union[list,tuple,np.ndarray]] = None,
        sigma_excitation_error:float = 0.02,
        tol_excitation_error_mult:float = 3,
        tol_intensity:float = 0.1
        ):
        """
        Generate a single diffraction pattern, return all peaks as a pointlist.

        Args:
            zone_axis (np float vector):     3 element projection direction for sim pattern
                                             Can also be a 3x3 orientation matrix (zone axis 3rd column)
            foil_normal:                     3 element foil normal - set to None to use zone_axis
            proj_x_axis (np float vector):   3 element vector defining image x axis (vertical)
            sigma_excitation_error (float): sigma value for envelope applied to s_g (excitation errors) in units of Angstroms
            tol_excitation_error_mult (float): tolerance in units of sigma for s_g inclusion
            tol_intensity (np float):        tolerance in intensity units for inclusion of diffraction spots

        Returns:
            bragg_peaks (PointList):         list of all Bragg peaks with fields [qx, qy, intensity, h, k, l]
        """

        zone_axis = np.asarray(zone_axis)

        if zone_axis.ndim == 1:
            zone_axis = np.asarray(zone_axis)
        elif zone_axis.shape == (3,3):
            proj_x_axis = zone_axis[:,0]
            zone_axis = zone_axis[:,2]
        else:
            proj_x_axis = zone_axis[:,0,0]
            zone_axis = zone_axis[:,2,0]

        # Foil normal
        if foil_normal is None:
            foil_normal = zone_axis
        else:
            foil_normal = np.asarray(foil_normal)
        foil_normal = foil_normal / np.linalg.norm(foil_normal)

        # Logic to set x axis for projected images
        if proj_x_axis is None:
            if np.all(zone_axis == np.array([-1,0,0])):
                proj_x_axis = np.array([0,-1,0])
            elif np.all(zone_axis == np.array([1,0,0])):
                proj_x_axis = np.array([0,1,0])
            else:
                proj_x_axis = np.array([-1,0,0])


        # wavevector
        zone_axis_norm = zone_axis / np.linalg.norm(zone_axis)
        k0 = zone_axis_norm / self.wavelength

        # Excitation errors
        cos_alpha = np.sum((k0[:,None] + self.g_vec_all) * foil_normal[:,None], axis=0) \
            / np.linalg.norm(k0[:,None] + self.g_vec_all, axis=0)
        sg = (-0.5) * np.sum((2*k0[:,None] + self.g_vec_all) * self.g_vec_all, axis=0) \
            / (np.linalg.norm(k0[:,None] + self.g_vec_all, axis=0)) / cos_alpha

        # Threshold for inclusion in diffraction pattern
        sg_max = sigma_excitation_error * tol_excitation_error_mult
        keep = np.abs(sg) <= sg_max
        g_diff = self.g_vec_all[:,keep]
        
        # Diffracted peak intensities and labels
        g_int = self.struct_factors_int[keep] \
            * np.exp(sg[keep]**2/(-2*sigma_excitation_error**2))
        hkl = self.hkl[:, keep]

        # Intensity tolerance
        keep_int = g_int > tol_intensity

        # Diffracted peak locations
        ky_proj = np.cross(zone_axis, proj_x_axis)
        kx_proj = np.cross(ky_proj, zone_axis)

        kx_proj = kx_proj / np.linalg.norm(kx_proj)
        ky_proj = ky_proj / np.linalg.norm(ky_proj)
        gx_proj = np.sum(g_diff[:,keep_int] * kx_proj[:,None], axis=0)
        gy_proj = np.sum(g_diff[:,keep_int] * ky_proj[:,None], axis=0)

        # Diffracted peak labels
        h = hkl[0, keep_int]
        k = hkl[1, keep_int]
        l = hkl[2, keep_int]

        # Output as PointList
        bragg_peaks = PointList([
            ('qx','float64'),
            ('qy','float64'),
            ('intensity','float64'),
            ('h','int'),
            ('k','int'),
            ('l','int')])
        bragg_peaks.add_pointarray(np.vstack((
            gx_proj, 
            gy_proj, 
            g_int[keep_int],
            h,
            k,
            l)).T)

        return bragg_peaks



    def plot_orientation_maps(
        self,
        orientation_matrices:np.ndarray,
        corr_all:Optional[np.ndarray]=None,
        corr_range:np.ndarray=np.array([0, 5]),
        orientation_index_plot:int = 0,
        orientation_rotate_xy:bool=None,
        scale_legend:bool = None,
        corr_normalize:bool=True,
        figsize:Union[list,tuple,np.ndarray]=(20,5),
        figlayout:Union[list,tuple,np.ndarray] = np.array([1,4]),
        returnfig:bool=False):
        """
        Generate and plot the orientation maps

        Args:
            orientation_zone_axis_range(float):     numpy array (3,3) where the 3 rows are the basis vectors for the orientation triangle
            orientation_matrices (float):   numpy array containing orientations, with size (Rx, Ry, 3, 3) or (Rx, Ry, 3, 3, num_matches)
            corr_all(float):                numpy array containing the correlation values to use as a mask
            orientation_index_plot (int):   index of orientations to plot
            orientation_rotate_xy (float):  rotation in radians for the xy directions of plots
            scale_legend (float):           2 elements, x and y scaling of legend panel
            figlayout (int)                 2 elements giving the # of rows and columns for the figure.  
                                            Must be [1, 4], [2, 2] or [4,1] currently.
            returnfig (bool):               set to True to return figure and axes handles

        Returns:
            images_orientation (int):       RGB images 
            fig, axs (handles):             Figure and axes handes for the 
        
        NOTE:
            Currently, no symmetry reduction.  Therefore the x and y orientations
            are going to be correct only for [001][011][111] orientation triangle.

        """


        # Inputs
        # Legend size
        leg_size = np.array([300,300],dtype='int')

        # Color of the 3 corners
        color_basis = np.array([
            [1.0, 0.0, 0.0],
            [0.0, 0.7, 0.0],
            [0.0, 0.3, 1.0],
            ])

        # Basis for fitting
        A = self.orientation_zone_axis_range.T

        # initalize image arrays
        images_orientation = np.zeros((
            orientation_matrices.shape[0],
            orientation_matrices.shape[1],
            3,3))

        # in-plane rotation array if needed
        if orientation_rotate_xy is not None:
            m = np.array([
                [np.cos(orientation_rotate_xy), -np.sin(orientation_rotate_xy), 0],
                [np.sin(orientation_rotate_xy), np.cos(orientation_rotate_xy), 0],
                [0,0,1]])



        # loop over all pixels and calculate weights
        for ax in range(orientation_matrices.shape[0]):
            for ay in range(orientation_matrices.shape[1]):
                if orientation_matrices.ndim == 4:
                    orient = orientation_matrices[ax,ay,:,:]
                else:
                    orient = orientation_matrices[ax,ay,:,:,orientation_index_plot]

                # Rotate in-plane if needed
                if orientation_rotate_xy is not None:
                    orient = m @ orient


                for a0 in range(3):
                    # w = np.linalg.solve(A,orient[:,a0])
                    w = np.linalg.solve(
                        A,
                        np.sort(np.abs(orient[:,a0])))
                    w = w / (1 - np.exp(-np.max(w)))
                    # np.max(w)

                    rgb = color_basis[0,:] * w[0] \
                        + color_basis[1,:] * w[1] \
                        + color_basis[2,:] * w[2]

                    images_orientation[ax,ay,:,a0] = rgb

        # clip range
        images_orientation = np.clip(images_orientation,0,1)


        # Masking
        if corr_all is not None:
            if orientation_matrices.ndim == 4:
                if corr_normalize:
                    mask = corr_all / np.mean(corr_all)
                else:
                    mask = corr_all
            else:
                if corr_normalize:
                    mask = corr_all[:,:,orientation_index_plot] / np.mean(corr_all[:,:,orientation_index_plot])
                else:
                    mask = corr_all[:,:,orientation_index_plot]
                

            mask = (mask - corr_range[0]) / (corr_range[1] - corr_range[0])
            mask = np.clip(mask,0,1)

            for a0 in range(3):
                for a1 in range(3):
                    images_orientation[:,:,a0,a1] *=  mask

        # Draw legend
        x = np.linspace(0,1,leg_size[0])
        y = np.linspace(0,1,leg_size[1])
        ya,xa=np.meshgrid(y,x)
        mask_legend = np.logical_and(2*xa > ya, 2*xa < 2-ya) 
        w0 = 1-xa - 0.5*ya
        w1 = xa - 0.5*ya
        w2 = ya

        w_scale = np.maximum(np.maximum(w0,w1),w2)
        # w_scale = w0 + w1 + w2
        # w_scale = (w0**4 + w1**4 + w2**4)**0.25
        w_scale = 1 - np.exp(-w_scale)
        w0 = w0 / w_scale # * mask_legend
        w1 = w1 / w_scale # * mask_legend
        w2 = w2 / w_scale # * mask_legend
        
        im_legend = np.zeros((
            leg_size[0],
            leg_size[1],
            3))
        for a0 in range(3):
            im_legend[:,:,a0] = \
                w0*color_basis[0,a0] + \
                w1*color_basis[1,a0] + \
                w2*color_basis[2,a0]
            im_legend[:,:,a0] *= mask_legend
            im_legend[:,:,a0] += 1-mask_legend
        im_legend = np.clip(im_legend,0,1)

        # plotting
        if figlayout[0] == 1 and figlayout[1] == 4:
            fig, ax = plt.subplots(1, 4, figsize=figsize)
        elif figlayout[0] == 2 and figlayout[1] == 2:
            fig, ax = plt.subplots(2, 2, figsize=figsize)
            ax = np.array([
                ax[0,0],
                ax[0,1],
                ax[1,0],
                ax[1,1],
                ])
        elif figlayout[0] == 4 and figlayout[1] == 1:
            fig, ax = plt.subplots(4, 1, figsize=figsize)


        ax[0].imshow(images_orientation[:,:,:,0])
        ax[1].imshow(images_orientation[:,:,:,1])
        ax[2].imshow(images_orientation[:,:,:,2])

        ax[0].set_title(
            'Orientation of x-axis',
            size=20)
        ax[1].set_title(
            'Orientation of y-axis',
            size=20)
        ax[2].set_title(
            'Zone Axis',
            size=20) 
        ax[0].xaxis.tick_top()
        ax[1].xaxis.tick_top()
        ax[2].xaxis.tick_top()

        # Legend
        ax[3].imshow(im_legend,
            aspect='auto')

        label_0 = self.orientation_zone_axis_range[0,:]
        label_0 = np.round(label_0 * 1e3) * 1e-3
        label_0 /= np.min(np.abs(label_0[np.abs(label_0)>0]))

        label_1 = self.orientation_zone_axis_range[1,:]
        label_1 = np.round(label_1 * 1e3) * 1e-3
        label_1 /= np.min(np.abs(label_1[np.abs(label_1)>0]))

        label_2 = self.orientation_zone_axis_range[2,:]
        label_2 = np.round(label_2 * 1e3) * 1e-3
        label_2 /= np.min(np.abs(label_2[np.abs(label_2)>0]))
        

        ax[3].yaxis.tick_right()
        ax[3].set_yticks([(leg_size[0]-1)/2])
        ax[3].set_yticklabels([
            str(label_2)])

        ax3a = ax[3].twiny()
        ax3b = ax[3].twiny()

        ax3a.set_xticks([0])
        ax3a.set_xticklabels([
            str(label_0)])
        ax3a.xaxis.tick_top()
        ax3b.set_xticks([0])
        ax3b.set_xticklabels([
            str(label_1)])
        ax3b.xaxis.tick_bottom()
        ax[3].set_xticks([])

        # ax[3].xaxis.label.set_color('none')
        ax[3].spines['left'].set_color('none')
        ax[3].spines['right'].set_color('none')
        ax[3].spines['top'].set_color('none')
        ax[3].spines['bottom'].set_color('none')
        
        ax3a.spines['left'].set_color('none')
        ax3a.spines['right'].set_color('none')
        ax3a.spines['top'].set_color('none')
        ax3a.spines['bottom'].set_color('none')
        
        ax3b.spines['left'].set_color('none')
        ax3b.spines['right'].set_color('none')
        ax3b.spines['top'].set_color('none')
        ax3b.spines['bottom'].set_color('none')
        
        ax[3].tick_params(labelsize=16)
        ax3a.tick_params(labelsize=16)
        ax3b.tick_params(labelsize=16)


        if scale_legend is not None:
            pos = ax[3].get_position()
            pos_new = [
                pos.x0, 
                pos.y0 + pos.height*(1 - scale_legend[1])/2,
                pos.width*scale_legend[0], 
                pos.height*scale_legend[1],
                ] 
            ax[3].set_position(pos_new) 
      

        if returnfig:
            return images_orientation, fig, ax
        else:
            return images_orientation


def plot_diffraction_pattern(
    bragg_peaks:PointList,
    bragg_peaks_compare:PointList=None,
    scale_markers:float=10,
    scale_markers_compare:Optional[float]=None,
    power_markers:float=1,
    plot_range_kx_ky:Optional[Union[list,tuple,np.ndarray]]=None,
    add_labels:bool=True,
    shift_labels:float=0.08,
    shift_marker:float = 0.005,
    min_marker_size:float = 1e-6,
    figsize:Union[list,tuple,np.ndarray]=(8,8),
    returnfig:bool=False,
    input_fig_handle=None):
    """
    2D scatter plot of the Bragg peaks

    Args:
        bragg_peaks (PointList):        numpy array containing ('qx', 'qy', 'intensity', 'h', 'k', 'l')
        bragg_peaks_compare(PointList): numpy array containing ('qx', 'qy', 'intensity')
        scale_markers (float):          size scaling for markers
        scale_markers_compare (float):  size scaling for markers of comparison
        power_markers (float):          power law scaling for marks (default is 1, i.e. amplitude)
        plot_range_kx_ky (float):       2 element numpy vector giving the plot range
        add_labels (bool):              flag to add hkl labels to peaks
        min_marker_size (float):        minimum marker size for the comparison peaks
        figsize (2 element float):      size scaling of figure axes
        returnfig (bool):               set to True to return figure and axes handles
    """

    # 2D plotting
    if input_fig_handle is None:
        # fig = plt.figure(figsize=figsize)
        # ax = fig.add_subplot()
        fig, ax = plt.subplots(1,1,figsize=figsize)
    else:
        fig = input_fig_handle[0]
        ax_parent = input_fig_handle[1]
        ax = ax_parent[0]

    if power_markers == 2:
        marker_size = scale_markers*bragg_peaks.data['intensity']
    else:
        marker_size = scale_markers*(bragg_peaks.data['intensity']**(power_markers/2))

    if bragg_peaks_compare is None:
        ax.scatter(
            bragg_peaks.data['qy'], 
            bragg_peaks.data['qx'], 
            s=marker_size,
            facecolor='k')
    else:
        if scale_markers_compare is None:
            scale_markers_compare = scale_markers

        if power_markers == 2:
            marker_size_compare = np.maximum(
                scale_markers_compare*bragg_peaks_compare.data['intensity'], min_marker_size)
        else:
            marker_size_compare = np.maximum(
                scale_markers_compare*(bragg_peaks_compare.data['intensity']**(power_markers/2)), min_marker_size)

        ax.scatter(
            bragg_peaks_compare.data['qy'], 
            bragg_peaks_compare.data['qx'], 
            s=marker_size_compare,
            marker='o',
            facecolor=[0.0,0.7,1.0])
        ax.scatter(
            bragg_peaks.data['qy'], 
            bragg_peaks.data['qx'], 
            s=marker_size,
            marker='+',
            facecolor='k')


    if plot_range_kx_ky is not None:
        ax.set_xlim((-plot_range_kx_ky[0],plot_range_kx_ky[0]))
        ax.set_ylim((-plot_range_kx_ky[1],plot_range_kx_ky[1]))

    ax.invert_yaxis()
    ax.set_box_aspect(1)
    ax.xaxis.tick_top()

    # Labels for all peaks
    if add_labels is True:
        text_params = {
            'ha': 'center',
            'va': 'center',
            'family': 'sans-serif',
            'fontweight': 'normal',
            'color': 'r',
            'size': 10}

        def overline(x):
            return str(x) if np.abs(x) >= 0 else '$\overline{" + str(np.abs(x)) + "}$'

        for a0 in np.arange(bragg_peaks.data.shape[0]):
            h = bragg_peaks.data['h'][a0]
            k = bragg_peaks.data['k'][a0]
            l = bragg_peaks.data['l'][a0]

            ax.text( \
                bragg_peaks.data['qy'][a0],
                bragg_peaks.data['qx'][a0] - shift_labels - shift_marker*np.sqrt(marker_size[a0]),
                f'{overline(h)}{overline(k)}{overline(l)}',
                **text_params)  

    if input_fig_handle is None:
        plt.show()

    if returnfig:
        return fig, ax





def axisEqual3D(ax):
    extents = np.array([getattr(ax, 'get_{}lim'.format(dim))() for dim in 'xyz'])
    sz = extents[:,1] - extents[:,0]
    centers = np.mean(extents, axis=1)
    maxsize = max(abs(sz))
    r = maxsize/2
    for ctr, dim in zip(centers, 'xyz'):
        getattr(ax, 'set_{}lim'.format(dim))(ctr - r, ctr + r)


def atomic_colors(ID):
    return {
        1:    np.array([0.8,0.8,0.8]),
        2:    np.array([1.0,0.7,0.0]),
        3:    np.array([1.0,0.0,1.0]),
        4:    np.array([0.0,0.5,0.0]),
        5:    np.array([0.5,0.0,0.0]),
        6:    np.array([0.5,0.5,0.5]),
        7:    np.array([0.0,0.7,1.0]),
        8:    np.array([1.0,0.0,0.0]),
        13:   np.array([0.6,0.7,0.8]),
        14:   np.array([0.3,0.3,0.3]),
        15:   np.array([1.0,0.6,0.0]),
        16:   np.array([1.0,0.9,0.0]),
        17:   np.array([0.0,1.0,0.0]),
        79:   np.array([1.0,0.7,0.0]),
    }.get(ID, np.array([0.0,0.0,0.0]))
