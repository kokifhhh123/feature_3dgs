import os
import cv2
import time
import torch
import dsacstar
import numpy as np
from pathlib import Path
from data.ace.dataset import CamLocDataset
from data.ace.ace_network import Regressor
from argparse import ArgumentParser
import utils.loc.loc_utils as loc_utils
from torch.cuda.amp import autocast
from scene_ori import Scene, GaussianModel
from torch.utils.data import DataLoader
from utils.graphics_utils import fov2focal
from utils.loc.depth import project_2d_to_3d
from gaussian_renderer.__init__ori import render
from matchers.lightglue import LightGlue
from encoders.superpoint.superpoint import SuperPoint
from utils.loc.pycolmap_utils import opencv_to_pycolmap_pnp
from arguments import ModelParams, PipelineParams, get_combined_args
from matchers.mast3r.mast3r.model import AsymmetricMASt3R
from matchers.LoFTR.utils.utils import load_LoFTR
from z_scannet1500.visual import get_aspan


def localize_set(model_path, name, views, gaussians, pipe_param, background, 
                 args, encoder, matcher, ace_network, ace_test_loader):
    rErrs = []
    tErrs = []
    prior_rErr = []
    prior_tErr = []
    total_elapsed_time = 0
    device = torch.device("cuda")
    scene_name = model_path.split('/')[-3]
    test_name = args.test_name
    print(scene_name)
    if args.save_match:
        match_folder = f'{model_path}/match_imgs/{test_name}'
        os.makedirs(match_folder, exist_ok=True)
    with torch.no_grad():
        for index, (image_B1HW, _, gt_pose_B44, _, intrinsics_B33, _, _, filenames) in enumerate(ace_test_loader):
            view = views[index]
            start = time.time()
            gt_im = view.original_image[0:3, :, :].cuda().unsqueeze(0)
            image_B1HW = image_B1HW.to(device, non_blocking=True)
            K = np.eye(3)
            focal_length = fov2focal(view.FoVx, view.image_width)
            K[0, 0] = K[1, 1] = focal_length
            K[0, 2] = view.image_width / 2
            K[1, 2] = view.image_height / 2
            gt_R = view.R # c2w rotation
            gt_t = view.T # w2c translation
            # Predict scene coordinates.
            with autocast(enabled=True):
                scene_coordinates_B3HW = ace_network(image_B1HW)
            scene_coordinates_B3HW = scene_coordinates_B3HW.float().cpu()

            for _, (scene_coordinates_3HW, gt_pose_44, intrinsics_33, frame_path) in \
                            enumerate(zip(scene_coordinates_B3HW, 
                                          gt_pose_B44, intrinsics_B33, filenames)):
                print(f"{index}, {view.image_name}")
                focal_length = intrinsics_33[0, 0].item()
                ppX = intrinsics_33[0, 2].item()
                ppY = intrinsics_33[1, 2].item()
                assert torch.allclose(intrinsics_33[0, 0], intrinsics_33[1, 1])
                
                frame_name = Path(frame_path).name
                out_pose = torch.zeros((4, 4))

                inlier_count = dsacstar.forward_rgb(
                    scene_coordinates_3HW.unsqueeze(0),
                    out_pose,
                    64,
                    10,
                    focal_length,
                    ppX,
                    ppY,
                    100,
                    100,
                    ace_network.OUTPUT_SUBSAMPLE,
                )
                # out_R = out_pose[0:3, 0:3].numpy()
                # out_t = out_pose[0:3, 3].numpy()
                rotError, transError = loc_utils.calculate_pose_errors_ace(
                    gt_pose_44, out_pose)

                out_R = out_pose[0:3, 0:3].numpy() # c2w rotation
                out_t = out_pose[0:3, 3].numpy() # c2w translation

                R_inv = out_R.T # w2c rotation
                t_inv = -R_inv @ out_t # w2c translation
                w2c = torch.eye(4, 4, device='cuda')
                w2c[:3, :3] = torch.from_numpy(R_inv).float()
                w2c[:3, 3] = torch.from_numpy(t_inv).float()
                view.update_RT(out_R, t_inv)

                render_pkg = render(view, gaussians, pipe_param, background)
                db_render = render_pkg["render"]
                db_depth = render_pkg["depth"]
                query_render = gt_im
                result = None
                if args.match_type==0:
                    result = loc_utils.img_match_rival(query_render, db_render, encoder, matcher)
                if args.match_type==1:
                    rotError_final, transError_final = loc_utils.img_match_mast3r(query_render, db_render, matcher, 
                                                             K, depth_map=db_depth, w2c=w2c, gt_pose_44=gt_pose_44)
                if args.match_type==2:
                    result = loc_utils.img_match_aspan(query_render, db_render, matcher,)
                print(f"time 7: {time.time()-start}")
                if result is not None:
                    if result is None:
                        prior_rErr.append(rotError)
                        prior_tErr.append(transError)
                        rErrs.append(rotError)
                        tErrs.append(transError)
                        print(f"Rotation Error: {rotError} deg")
                        print(f"Translation Error: {transError} cm")
                        continue
                    if not len(result['mkpt1'].cpu())>args.stop_kpt_num:
                        prior_rErr.append(rotError)
                        prior_tErr.append(transError)
                        rErrs.append(rotError)
                        tErrs.append(transError)
                        print(f"Rotation Error: {rotError} deg")
                        print(f"Translation Error: {transError} cm")
                        total_elapsed_time += time.time()-start
                        continue
                    db_world = project_2d_to_3d(result['mkpt1'].cpu(), db_depth.cpu(), 
                                                torch.tensor(K, dtype=torch.float32).cpu(), 
                                                w2c.cpu()).cpu().numpy().astype(np.float64)
                    q_matched = result['mkpt0'].cpu().numpy().astype(np.float64)
                    if args.pnp == "iters":
                        _, R_final, t_final, _ = cv2.solvePnPRansac(db_world, q_matched, K, distCoeffs=None, 
                                                                    flags=cv2.SOLVEPNP_ITERATIVE, 
                                                                    iterationsCount=args.ransac_iters)
                        R_final, _ = cv2.Rodrigues(R_final)
                    elif args.pnp == "epnp":
                        _, R_final, t_final, _ = cv2.solvePnPRansac(db_world, q_matched, K, distCoeffs=None, 
                                                                    flags=cv2.SOLVEPNP_EPNP)
                        R_final, _ = cv2.Rodrigues(R_final)
                    elif args.pnp == "pycolmap":
                        R_final, t_final = opencv_to_pycolmap_pnp(db_world, q_matched, K, 
                                                            view.image_width, view.image_height)
                    rotError_final, transError_final = loc_utils.calculate_pose_errors(gt_R, gt_t, R_final.T, t_final)

                    if args.save_match:
                        result['img0'] = gt_im.squeeze(0).permute(1, 2, 0)
                        result['img1'] = db_render.squeeze(0).permute(1, 2, 0)
                        loc_utils.save_matchimg(result, 
                            f'{match_folder}/{index}_{view.image_name}__(T:{transError:.2f}_R:{rotError:.2f})__(T:{transError_final:.2f}_R:{rotError_final:.2f}).png')
                
                # print(index)
                print(f"Rotation Error: {rotError} deg")
                print(f"Translation Error: {transError} cm")
                # Print the errors
                print(f"Final Rotation Error: {rotError_final} deg")
                print(f"Final Translation Error: {transError_final} cm")
                elapsed_time = time.time()-start
                total_elapsed_time += elapsed_time
                print(f"elapsed time: {elapsed_time}")
                print()
                # breakpoint()
                prior_rErr.append(rotError)
                prior_tErr.append(transError)
                rErrs.append(rotError_final)
                tErrs.append(transError_final)
    
    error_foler = f'{model_path}/error_logs/{test_name}'
    os.makedirs(error_foler, exist_ok=True)
    mean_elapsed_time = total_elapsed_time / len(rErrs)
    print('rot len: ',len(prior_rErr))
    print('final rot len: ', len(rErrs))
    print('mean elapsed time: ', mean_elapsed_time)
    print()
    loc_utils.log_errors(error_foler, name, prior_rErr, prior_tErr, list_text=f"prior", error_text="prior_final")
    loc_utils.log_errors(error_foler, name, rErrs, tErrs, list_text="warp", error_text="prior_final",
                         elapsed_time=mean_elapsed_time)


def localize(model_param:ModelParams, pipe_param:PipelineParams, args):
    gaussians = GaussianModel(model_param.sh_degree)
    scene = Scene(model_param, gaussians, load_iteration=args.iteration, shuffle=False, load_feature=False)
    bg_color = [1,1,1] if model_param.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")
    conf = {
        "sparse_outputs": True,
        "dense_outputs": False,
        "max_num_keypoints": 1024,
        "detection_threshold": args.sp_th,
    }
    if args.match_type==0:
        encoder = SuperPoint(conf).cuda().eval()
        matcher = LightGlue({"filter_threshold": args.lg_th ,}).cuda().eval()
    elif args.match_type==1:
        model_name = "naver/MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric"
        encoder = None
        matcher = AsymmetricMASt3R.from_pretrained(model_name).cuda().eval()
    elif args.match_type==2:
        encoder = None
        matcher = get_aspan(name="indoor")
    
    encoder_state_dict = torch.load(args.ace_encoder_path, map_location="cpu")
    head_state_dict = torch.load(args.ace_ckpt, map_location="cpu")
    ace_network = Regressor.create_from_split_state_dict(encoder_state_dict, head_state_dict).cuda().eval()
    ########################################################
    scene_path = Path(args.source_path).parent
    ########################################################
    testset = CamLocDataset(
        scene_path / "test",
        mode=0,  # Default for ACE, we don't need scene coordinates/RGB-D.
        image_height=480,
    )
    testset_loader = DataLoader(testset, shuffle=False, num_workers=0)
    localize_set(model_param.model_path, "test", scene.getTestCameras(), 
                 gaussians, pipe_param, background, args, encoder, matcher, ace_network, testset_loader)


if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Testing script parameters")
    Model_param = ModelParams(parser, sentinel=True)
    Pipe_param = PipelineParams(parser)
    parser.add_argument("--iteration", default=-1, type=int)
    parser.add_argument("--ransac_iters", default=20000, type=int)
    parser.add_argument("--save_match", action='store_true', help='Save match if this flag is provided.')
    parser.add_argument("--sp_th", default=0.01, type=float)
    parser.add_argument("--lg_th", default=0.01, type=float)
    parser.add_argument("--stop_kpt_num", default=30, type=int)
    parser.add_argument("--ace_ckpt", type=str)
    parser.add_argument("--pnp", default="iters", type=str)
    parser.add_argument("--test_name", required=True, type=str)
    parser.add_argument("--match_type", default=0, type=int)
    parser.add_argument("--ace_encoder_path", 
                        default="/home/koki/code/cc/feature_3dgs_2/ace/ace_encoder_pretrained.pt", type=str)
    args = get_combined_args(parser)
    localize(Model_param.extract(args), Pipe_param.extract(args), args)
