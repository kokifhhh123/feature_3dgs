#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#
import os
import sys
import json
import torch
import random
import numpy as np
from PIL import Image
from pathlib import Path
from typing import NamedTuple
from utils.sh_utils import SH2RGB
from plyfile import PlyData, PlyElement
from scene.gaussian.gaussian_model import BasicPointCloud
from utils.graphics_utils import getWorld2View2, focal2fov, fov2focal
from scene.colmap_loader import read_extrinsics_text, read_intrinsics_text, qvec2rotmat, \
                                read_extrinsics_binary, read_intrinsics_binary, read_points3D_binary, \
                                read_points3D_text, read_points3D_nvm

class CameraInfo(NamedTuple):
    uid: int
    R: np.array
    T: np.array
    FovY: np.array
    FovX: np.array
    focal_length: float
    image: np.array
    image_path: str
    image_name: str
    width: int
    height: int
    intrinsic_params: np.array
    intrinsic_model: str

class SceneInfo(NamedTuple):
    point_cloud: BasicPointCloud
    train_cameras: list
    test_cameras: list
    nerf_normalization: dict
    ply_path: str
    semantic_feature_dim: int


def getNerfppNorm(cam_info):
    def get_center_and_diag(cam_centers):
        cam_centers = np.hstack(cam_centers)
        avg_cam_center = np.mean(cam_centers, axis=1, keepdims=True)
        center = avg_cam_center
        dist = np.linalg.norm(cam_centers - center, axis=0, keepdims=True)
        diagonal = np.max(dist)
        return center.flatten(), diagonal
    cam_centers = []
    for cam in cam_info:
        W2C = getWorld2View2(cam.R, cam.T)
        C2W = np.linalg.inv(W2C)
        cam_centers.append(C2W[:3, 3:4])
    center, diagonal = get_center_and_diag(cam_centers)
    radius = diagonal * 1.1
    translate = -center
    return {"translate": translate, "radius": radius}


def readColmapCameras(cam_extrinsics, cam_intrinsics, images_folder, semantic_feature_folder, load_feature):
    cam_infos = []
    for idx, key in enumerate(cam_extrinsics):
        sys.stdout.write('\r')
        # the exact output you're looking for:
        sys.stdout.write("Reading camera {}/{}".format(idx+1, len(cam_extrinsics)))
        sys.stdout.flush()
        extr = cam_extrinsics[key]
        intr = cam_intrinsics[extr.camera_id]
        height = intr.height
        width = intr.width
        uid = intr.id
        R = np.transpose(qvec2rotmat(extr.qvec))
        T = np.array(extr.tvec)
        if intr.model=="SIMPLE_PINHOLE" or intr.model=="SIMPLE_RADIAL":
            focal_length_x = intr.params[0]
            FovY = focal2fov(focal_length_x, height)
            FovX = focal2fov(focal_length_x, width)
        ### elif intr.model=="PINHOLE":
        elif intr.model=="PINHOLE" or intr.model=="OPENCV":
            focal_length_x = intr.params[0]
            focal_length_y = intr.params[1]
            FovY = focal2fov(focal_length_y, height)
            FovX = focal2fov(focal_length_x, width)
        else:
            assert False, "Colmap camera model not handled: only undistorted datasets (PINHOLE or SIMPLE_PINHOLE cameras) supported!"
        image_path = os.path.join(images_folder, os.path.basename(extr.name))
        if not os.path.exists(image_path):
            image_path = os.path.join(images_folder, extr.name.replace('/', '-'))
        image_name = os.path.basename(image_path).split(".")[0]
        image = Image.open(image_path)
        # feature_name = os.path.basename(semantic_feature_folder)
        semantic_feature_path = os.path.join(semantic_feature_folder, image_name) + '_fmap.pt'
        semantic_feature_name = os.path.basename(semantic_feature_path).split(".")[0]
        if os.path.exists(semantic_feature_path) and load_feature:
        # try:
            semantic_feature = torch.load(semantic_feature_path) if os.path.exists(semantic_feature_path) else None
        # except FileNotFoundError:
        else:
            semantic_feature = None
        score_feature_path = os.path.join(semantic_feature_folder, image_name) + '_smap.pt'
        score_feature_name = os.path.basename(score_feature_path).split(".")[0]
        if os.path.exists(score_feature_path) and load_feature:
            score_feature = torch.load(score_feature_path) if os.path.exists(score_feature_path) else None
        else:
            score_feature = None
        cam_info = CameraInfo(uid=uid, R=R, T=T, FovY=FovY, FovX=FovX, image=image,
                            image_path=image_path, image_name=image_name,
                            intrinsic_params=intr.params,
                            intrinsic_model=intr.model,
                            width=width, height=height,
                            semantic_feature=semantic_feature,           score_feature = score_feature,
                            semantic_feature_path=semantic_feature_path, score_feature_path = score_feature_path, 
                            semantic_feature_name=semantic_feature_name, score_feature_name = score_feature_name)
        cam_infos.append(cam_info)
    sys.stdout.write('\n')
    return cam_infos


def fetchPly(path):
    plydata = PlyData.read(path)
    vertices = plydata['vertex']
    positions = np.vstack([vertices['x'], vertices['y'], vertices['z']]).T
    colors = np.vstack([vertices['red'], vertices['green'], vertices['blue']]).T / 255.0
    normals = np.vstack([vertices['nx'], vertices['ny'], vertices['nz']]).T
    return BasicPointCloud(points=positions, colors=colors, normals=normals)


def storePly(path, xyz, rgb):
    # Define the dtype for the structured array
    dtype = [('x', 'f4'), ('y', 'f4'), ('z', 'f4'),
            ('nx', 'f4'), ('ny', 'f4'), ('nz', 'f4'),
            ('red', 'u1'), ('green', 'u1'), ('blue', 'u1')]
    normals = np.zeros_like(xyz)

    elements = np.empty(xyz.shape[0], dtype=dtype)
    attributes = np.concatenate((xyz, normals, rgb), axis=1)
    elements[:] = list(map(tuple, attributes))
    # Create the PlyData object and write to file
    vertex_element = PlyElement.describe(elements, 'vertex')
    ply_data = PlyData([vertex_element])
    ply_data.write(path)


def readColmap_cams_params(intrinsic_folder, extrinsic_folder):
    intrinsic_files = os.listdir(intrinsic_folder)
    extrinsic_files = os.listdir(extrinsic_folder)
    # Read intrinsics
    with open(f"{intrinsic_folder}/{intrinsic_files[0]}", "r") as fid:
        K = float(fid.readline())

    w2cs = []
    # Read extrincsics
    for file in extrinsic_files:
        with open(f"{extrinsic_folder}/{file}", "r") as fid:
            c2w = []
            while True:
                line = fid.readline().rstrip()
                if not line:
                    break
                c2w+=line.split(' ')
        c2w = np.array([float(x) for x in c2w]).reshape((4,4))
        w2c = np.linalg.inv(c2w)
        # RTs
        w2cs.append(w2c)
    return K, w2cs


def readColmapSceneInfo(path: str, foundation_model: str, eval: bool, images=None, llffhold=8, 
                        load_feature=True, view_num=None, load_testcam=True):
    try:
        cameras_extrinsic_file = os.path.join(path, "sparse/0", "images.bin")
        cameras_intrinsic_file = os.path.join(path, "sparse/0", "cameras.bin")
        cam_extrinsics = read_extrinsics_binary(cameras_extrinsic_file)
        cam_intrinsics = read_intrinsics_binary(cameras_intrinsic_file)
    except:
        cameras_extrinsic_file = os.path.join(path, "sparse/0", "images.txt")
        cameras_intrinsic_file = os.path.join(path, "sparse/0", "cameras.txt")
        cam_extrinsics = read_extrinsics_text(cameras_extrinsic_file)
        cam_intrinsics = read_intrinsics_text(cameras_intrinsic_file)
    image_dir = f"{images}"
    semantic_feature_dir = f"{foundation_model}"
    cam_infos_unsorted = readColmapCameras(cam_extrinsics=cam_extrinsics, cam_intrinsics=cam_intrinsics, 
                                           images_folder=os.path.join(path, image_dir), 
                                           semantic_feature_folder=os.path.join(path, semantic_feature_dir),
                                           load_feature = load_feature)
    cam_infos = sorted(cam_infos_unsorted.copy(), key = lambda x : x.image_name)
    if cam_infos[0].semantic_feature is not None:
        semantic_feature_dim = cam_infos[0].semantic_feature.shape[0]
    else:
        semantic_feature_dim = 16
    if eval:
        train_cam_infos = [c for idx, c in enumerate(cam_infos) if idx % llffhold != 2] # avoid 1st to be test view
        test_cam_infos = [c for idx, c in enumerate(cam_infos) if idx % llffhold == 2]
        if view_num is not None:
            random.shuffle(train_cam_infos)
            random.shuffle(test_cam_infos)
            train_cam_infos = train_cam_infos[:view_num]
            test_cam_infos = test_cam_infos[:view_num]
    else:
        test_cam_infos_unsorted = []
        train_cam_infos = cam_infos
        test_cam_infos = []
        if load_testcam:
            t_path = str(Path(path).parent)
            test_images_folder = os.path.join(t_path, f"test/{images}")
            test_extrinsic_folder = os.path.join(t_path, "test/poses")
            test_intrinsic_folder = os.path.join(t_path, "test/calibration")
            test_feature_folder = os.path.join(t_path, f"test/{foundation_model}")
            test_views = os.listdir(test_images_folder)
            width, height = Image.open(f"{test_images_folder}/{test_views[0]}").size
            # K_test, w2cs_test = readColmap_cams_params(test_intrinsic_folder, test_extrinsic_folder)
            intrinsic_files = os.listdir(test_intrinsic_folder)
            with open(f"{test_intrinsic_folder}/{intrinsic_files[0]}", "r") as fid:
                K_test = float(fid.readline())
            for i, view in enumerate(test_views):
                sys.stdout.write('\r')
                sys.stdout.write(f"Reading {i+1} test / {len(test_views)} camera")
                sys.stdout.flush()
                v_name = view.split('.')[0]
                with open(f"{test_extrinsic_folder}/{v_name}.pose.txt", "r") as fid:
                    c2w = []
                    while True:
                        line = fid.readline().rstrip()
                        if not line:
                            break
                        c2w+=line.split(' ')
                c2w = np.array([float(x) for x in c2w]).reshape((4,4))
                w2c = np.linalg.inv(c2w)
                w2c_sample = w2c
                R = w2c_sample[:3,:3].T  # R is stored transposed due to 'glm' in CUDA code
                T = w2c_sample[:3, 3]
                focal_length_x = K_test
                FovY = focal2fov(focal_length_x, height)
                FovX = focal2fov(focal_length_x, width)
                image_path = os.path.join(test_images_folder, view)
                image_name = os.path.basename(image_path).split(".png")[0].split(".color")[0]
                image = Image.open(image_path)
                semantic_feature_path = os.path.join(test_feature_folder, image_name) + '_fmap.pt'
                semantic_feature_name = os.path.basename(semantic_feature_path).split(".")[0]
                if os.path.exists(semantic_feature_path) and load_feature:
                    semantic_feature = torch.load(semantic_feature_path)
                else:
                    semantic_feature = None
                score_feature_path = os.path.join(test_feature_folder, image_name) + '_smap.pt'
                score_feature_name = os.path.basename(score_feature_path).split(".")[0]
                if os.path.exists(score_feature_path) and load_feature:
                    score_feature = torch.load(score_feature_path)
                else:
                    score_feature = None
                cam_info = CameraInfo(uid=i, R=R, T=T, FovY=FovY, FovX=FovX, image=image,
                                    image_path=image_path, image_name=image_name,
                                    intrinsic_params=None,
                                    intrinsic_model=None,
                                    width=width, height=height,
                                    semantic_feature=semantic_feature,           score_feature = score_feature,
                                    semantic_feature_path=semantic_feature_path, score_feature_path = score_feature_path, 
                                    semantic_feature_name=semantic_feature_name, score_feature_name = score_feature_name)
                test_cam_infos_unsorted.append(cam_info)
            test_cam_infos = sorted(test_cam_infos_unsorted, key = lambda x : x.image_name)
            # test_cam_infos = test_cam_infos_unsorted
        if view_num is not None:
            random.shuffle(train_cam_infos)
            random.shuffle(test_cam_infos)
            train_cam_infos = train_cam_infos[:view_num]
            test_cam_infos = test_cam_infos[:view_num]
    nerf_normalization = getNerfppNorm(train_cam_infos)
    ply_path = os.path.join(path, "sparse/0/points3D.ply")
    bin_path = os.path.join(path, "sparse/0/points3D.bin")
    txt_path = os.path.join(path, "sparse/0/points3D.txt")
    if not os.path.exists(ply_path):
        print("Converting point3d.bin to .ply, will happen only the first time you open the scene.")
        try:
            xyz, rgb, _ = read_points3D_binary(bin_path)
        except:
            xyz, rgb, _ = read_points3D_text(txt_path)
        storePly(ply_path, xyz, rgb)
    try:
        pcd = fetchPly(ply_path)
    except:
        pcd = None
    scene_info = SceneInfo(point_cloud=pcd,
                           train_cameras=train_cam_infos,
                           test_cameras=test_cam_infos,
                           nerf_normalization=nerf_normalization,
                           ply_path=ply_path,
                           semantic_feature_dim=semantic_feature_dim) 
    return scene_info



def readCamerasFromTransforms(path, transformsfile, white_background, semantic_feature_folder, extension=".png"): 
    cam_infos = []
    with open(os.path.join(path, transformsfile)) as json_file:
        contents = json.load(json_file)
        fovx = contents["camera_angle_x"]
        frames = contents["frames"]
        for idx, frame in enumerate(frames):
            cam_name = os.path.join(path, frame["file_path"] + extension)
            c2w = np.array(frame["transform_matrix"])
            c2w[:3, 1:3] *= -1
            w2c = np.linalg.inv(c2w)
            R = np.transpose(w2c[:3,:3])  # R is stored transposed due to 'glm' in CUDA code
            T = w2c[:3, 3]
            image_path = os.path.join(path, cam_name)
            image_name = Path(cam_name).stem
            image = Image.open(image_path)
            im_data = np.array(image.convert("RGBA"))
            bg = np.array([1,1,1]) if white_background else np.array([0, 0, 0])
            norm_data = im_data / 255.0
            arr = norm_data[:,:,:3] * norm_data[:, :, 3:4] + bg * (1 - norm_data[:, :, 3:4])
            image = Image.fromarray(np.array(arr*255.0, dtype=np.byte), "RGB")
            fovy = focal2fov(fov2focal(fovx, image.size[0]), image.size[1])
            FovY = fovy 
            FovX = fovx
            semantic_feature_path = os.path.join(semantic_feature_folder, image_name) + '_fmap_CxHxW.pt' 
            semantic_feature_name = os.path.basename(semantic_feature_path).split(".")[0]
            semantic_feature = torch.load(semantic_feature_path)
            cam_infos.append(CameraInfo(uid=idx, R=R, T=T, FovY=FovY, FovX=FovX, image=image,
                              image_path=image_path, image_name=image_name, width=image.size[0], height=image.size[1],
                              semantic_feature=semantic_feature,
                              semantic_feature_path=semantic_feature_path,
                              semantic_feature_name=semantic_feature_name))
    return cam_infos



def readSplit_cams_params(intrinsic_folder, extrinsic_folder):
    intrinsic_files = sorted(os.listdir(intrinsic_folder))
    extrinsic_files = sorted(os.listdir(extrinsic_folder))
    with open(f"{intrinsic_folder}/{intrinsic_files[0]}", "r") as fid:
        K = float(fid.readline())
    w2cs = []
    for file in extrinsic_files:
        with open(f"{extrinsic_folder}/{file}", "r") as fid:
            c2w = []
            while True:
                line = fid.readline().rstrip()
                if not line:
                    break
                c2w+=line.split(' ')
        c2w = np.array([float(x) for x in c2w]).reshape((4,4))
        w2c = np.linalg.inv(c2w)
        w2cs.append(w2c)
    return K, w2cs



@torch.inference_mode()
def readSplitInfo(path, images, pcd = None, view_num=None, test_only_view_num=False, mlp_dim=16):
    train_images_folder = os.path.join(path, f"train/{images}")
    train_extrinsic_folder = os.path.join(path, "train/poses")
    train_intrinsic_folder = os.path.join(path, "train/calibration")
    
    test_images_folder = os.path.join(path, f"test/{images}")
    test_extrinsic_folder = os.path.join(path, "test/poses")
    test_intrinsic_folder = os.path.join(path, "test/calibration")

    ply_path = os.path.join(path, "out.ply")
    scene_name = path.split("_")[-1]
    if '7scenes' in path:
        sfm_path = os.path.join(f"/home/koki/code/cc/feature_3dgs_2/data/vis_loc/gsplatloc/7scenes_reference_models", 
                                scene_name, "old_gt_refined")
    elif 'Cambridge' in path:
        sfm_path = path
    else:
        raise ValueError(f"Unknown dataset: {path}")
    print(sfm_path)
    train_views = sorted(os.listdir(train_images_folder))
    test_views = sorted(os.listdir(test_images_folder))
    K_train, w2cs_train = readSplit_cams_params(train_intrinsic_folder, train_extrinsic_folder)
    K_test, w2cs_test = readSplit_cams_params(test_intrinsic_folder, test_extrinsic_folder)
    width, height = Image.open(f"{train_images_folder}/{train_views[0]}").size
    train_cam_infos_unsorted = []
    test_cam_infos_unsorted = []
    if view_num is not None:
        # train
        if not test_only_view_num:
            train_step = int(len(train_views)/view_num)
            x = []
            w2cs_x = []
            for i in range(0, len(train_views), train_step):
                x.append(train_views[i])
                w2cs_x.append(w2cs_train[i])
            train_views = x
            w2cs_train = w2cs_x
        # test
        test_step = int(len(test_views)/view_num)
        y = []
        w2cs_y = []
        for i in range(0, len(test_views), test_step):
            y.append(test_views[i])
            w2cs_y.append(w2cs_test[i])
        test_views = y
        w2cs_test = w2cs_y
    for i, view in enumerate(train_views):
        sys.stdout.write('\r')
        sys.stdout.write(f"Reading {i+1} train / {len(train_views)} camera")
        sys.stdout.flush()
        w2c_sample = w2cs_train[i]
        R = w2c_sample[:3,:3].T  # R is stored transposed due to 'glm' in CUDA code
        T = w2c_sample[:3, 3]
        focal_length_x = K_train
        FovY = focal2fov(focal_length_x, height)
        FovX = focal2fov(focal_length_x, width)
        image_path = os.path.join(train_images_folder, view)
        image_name = os.path.basename(image_path).split(".png")[0].split(".color")[0]
        image = Image.open(image_path)
        cam_info = CameraInfo(uid=i, R=R, T=T, 
                            FovY=FovY, FovX=FovX, focal_length=focal_length_x,
                            image=image,
                            image_path=image_path, image_name=image_name,
                            intrinsic_params=None,
                            intrinsic_model=None,
                            width=width, height=height,)
        train_cam_infos_unsorted.append(cam_info)
    for i, view in enumerate(test_views):
        sys.stdout.write('\r')
        sys.stdout.write(f"Reading {i+1} test / {len(test_views)} camera")
        sys.stdout.flush()
        w2c_sample = w2cs_test[i]
        R = w2c_sample[:3,:3].T  # R is stored transposed due to 'glm' in CUDA code
        T = w2c_sample[:3, 3]
        focal_length_x = K_test
        FovY = focal2fov(focal_length_x, height)
        FovX = focal2fov(focal_length_x, width)
        image_path = os.path.join(test_images_folder, view)
        image_name = os.path.basename(image_path).split(".png")[0].split(".color")[0]
        image = Image.open(image_path)
        cam_info = CameraInfo(uid=i, R=R, T=T, 
                              FovY=FovY, FovX=FovX, focal_length=focal_length_x,
                              image=image,
                            image_path=image_path, image_name=image_name,
                            intrinsic_params=None,
                            intrinsic_model=None,
                            width=width, height=height,)
        test_cam_infos_unsorted.append(cam_info)
    train_cam_infos = sorted(train_cam_infos_unsorted, key = lambda x : x.image_name)
    test_cam_infos = sorted(test_cam_infos_unsorted, key = lambda x : x.image_name)
    print(f"\nTotal cams: {len(train_cam_infos)+len(test_cam_infos)}")
    nerf_normalization = getNerfppNorm(train_cam_infos)
    if 'Cambridge' in path:
        nvm_path = os.path.join(sfm_path, "reconstruction.nvm")

        if not os.path.exists(ply_path):
            print("Converting reconstruction.nvm to .ply, will happen only the first time you open the scene.")
        try:
            xyz, rgb= read_points3D_nvm(nvm_path)
            storePly(ply_path, xyz, rgb)
        except:
            print("Error reading reconstruction.nvm file. Please ensure it exists and is in the correct format.")
    else:
        bin_path = os.path.join(sfm_path, "points3D.bin")
        if not os.path.exists(ply_path):
            print("Converting point3d.bin to .ply, will happen only the first time you open the scene.")
        try:
            xyz, rgb, _ = read_points3D_binary(bin_path)
            storePly(ply_path, xyz, rgb)
        except:
            print("Error reading reconstruction.nvm file. Please ensure it exists and is in the correct format.")
    
    try:
        pcd = fetchPly(ply_path)
    except:
        print("Error reading .ply file. Using default point cloud.")
        pcd = None
    
    scene_info = SceneInfo(point_cloud=pcd,
                           train_cameras=train_cam_infos,
                           test_cameras=test_cam_infos,
                           nerf_normalization=nerf_normalization,
                           ply_path=ply_path,
                           semantic_feature_dim=mlp_dim)
    return scene_info
