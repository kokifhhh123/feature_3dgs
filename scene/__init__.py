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
import json
import random
from arguments import ModelParams
import scene.dataset_readers as dataset_readers
from utils.system_utils import searchForMaxIteration
from scene.camera_utils import cameraList_from_camInfos, camera_to_JSON
from encoders.superpoint.superpoint import SuperPoint
from mlp.mlp import get_mlp_new

class Scene:
    def __init__(self, args:ModelParams, gaussians, load_iteration=None, 
                 resolution_scales=[1.0],  view_num=None, shuffle=True, 
                 load_train_cams=True, load_test_cams=True, 
                 load_feature=True,
                 test_only_view_num=False):
        """b
        :param path: Path to colmap scene main folder.
        """
        self.model_path = args.model_path
        self.loaded_iter = None
        self.gaussians = gaussians

        if load_iteration:
            if load_iteration == -1:
                self.loaded_iter = searchForMaxIteration(os.path.join(self.model_path, "point_cloud"))
            else:
                self.loaded_iter = load_iteration
            print("Loading trained model at iteration {}".format(self.loaded_iter))

        self.train_cameras = {}
        self.test_cameras = {}

        if os.path.exists(os.path.join(args.source_path, "train", "poses")):
            scene_info = dataset_readers.readSplitInfo(args.source_path, images=args.images, view_num=view_num,
                                                       mlp_dim=args.mlp_dim, test_only_view_num=test_only_view_num)
        elif os.path.exists(os.path.join(args.source_path, "sparse")):
            scene_info = dataset_readers.readColmapSceneInfo(path=args.source_path, foundation_model=args.foundation_model, 
                                                          eval=args.eval, images=args.images, view_num=view_num, 
                                                          load_feature = load_feature, load_testcam=load_test_cams,
                                                          )
        else:
            assert False, "Could not recognize scene type!"

        if not self.loaded_iter:
            with open(scene_info.ply_path, 'rb') as src_file, \
                open(os.path.join(self.model_path, "input.ply") , 'wb') as dest_file:
                dest_file.write(src_file.read())
            json_cams = []
            camlist = []
            if scene_info.test_cameras:
                camlist.extend(scene_info.test_cameras)
            if scene_info.train_cameras:
                camlist.extend(scene_info.train_cameras)
            for id, cam in enumerate(camlist):
                json_cams.append(camera_to_JSON(id, cam))
            with open(os.path.join(self.model_path, "cameras.json"), 'w') as file:
                json.dump(json_cams, file)

        if shuffle:
            random.shuffle(scene_info.train_cameras)  # Multi-res consistent random shuffling
            random.shuffle(scene_info.test_cameras)  # Multi-res consistent random shuffling
        self.cameras_extent = scene_info.nerf_normalization["radius"]
        conf = {
            "sparse_outputs": True,
            "dense_outputs": True,
            "max_num_keypoints": args.num_kpts,
            "detection_threshold": float(args.detect_th),
        }
        encoder = SuperPoint(conf).cuda().eval()
        mlp = get_mlp_new(dim=args.mlp_dim, name=args.mlp_name).cuda().eval()
        self.encoder = encoder
        self.mlp = mlp

        for resolution_scale in resolution_scales:
            if load_train_cams:
                print("Loading Training Cameras")
                self.train_cameras[resolution_scale] = cameraList_from_camInfos(scene_info.train_cameras, resolution_scale, args,
                                                                                encoder=encoder, mlp=mlp, load_feature=load_feature)
            if load_test_cams:
                print("Loading Test Cameras")
                self.test_cameras[resolution_scale] = cameraList_from_camInfos(scene_info.test_cameras, resolution_scale, args,
                                                                               encoder=encoder, mlp=mlp, load_feature=load_feature)

        if self.loaded_iter:
            self.gaussians.load_ply(os.path.join(self.model_path,
                                                           "point_cloud",
                                                           "iteration_" + str(self.loaded_iter),
                                                           "point_cloud.ply"))
        else:
            self.gaussians.create_from_pcd(scene_info.point_cloud, 
                                           self.cameras_extent, scene_info.semantic_feature_dim, args.speedup)

    def save(self, iteration):
        point_cloud_path = os.path.join(self.model_path, "point_cloud/iteration_{}".format(iteration))
        self.gaussians.save_ply(os.path.join(point_cloud_path, "point_cloud.ply"))

    def getTrainCameras(self, scale=1.0):
        return self.train_cameras[scale]

    def getTestCameras(self, scale=1.0):
        return self.test_cameras[scale]
