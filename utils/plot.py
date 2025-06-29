import os
import cv2
import torch
import argparse
import numpy as np
import torch.nn.functional as F
from matchers.aliked import ALIKED
from utils.utils import load_image2
from encoders.DISK.disk_kornia import DISK
from utils.scoremap_vis import one_channel_vis
from encoders.superpoint.superpoint import SuperPoint
from mlp.mlp import get_mlp_model, get_mlp_dataset, get_mlp_augment,\
                                    get_mlp_data_7scenes_Cambridege


def plot_points(img: torch.Tensor, kpts: torch.Tensor):
    C, H, W = img.shape
    x_cods = kpts[:, 0]
    y_cods = kpts[:, 1]
    x_floor = torch.floor(x_cods).long()
    y_floor = torch.floor(y_cods).long()
    indices0 = [
        (y_floor, x_floor),
        (y_floor, x_floor + 1),
        (y_floor + 1, x_floor),
        (y_floor + 1, x_floor + 1)
    ]
    indices1 = [
        (y_floor - 1, x_floor - 1),
        (y_floor - 1, x_floor + 0),
        (y_floor - 1, x_floor + 1),
        (y_floor - 1, x_floor + 2),

        (y_floor + 0 ,x_floor - 1),
        (y_floor + 0, x_floor + 2),
        (y_floor + 1 ,x_floor - 1),
        (y_floor + 1, x_floor + 2),
        
        (y_floor + 2, x_floor - 1),
        (y_floor + 2, x_floor + 0),
        (y_floor + 2, x_floor + 1),
        (y_floor + 2, x_floor + 2)
    ]
    for (yi, xi) in indices0:
        valid = (xi >= 0) & (xi < W) & (yi >= 0) & (yi < H)
        img[0, yi[valid], xi[valid]] = 1.0
        if C>1:
            img[1, yi[valid], xi[valid]] = 0.
            img[2, yi[valid], xi[valid]] = 0.
    # for (yi, xi) in indices1:
    #     valid = (xi >= 0) & (xi < W) & (yi >= 0) & (yi < H)
    #     img[0, yi[valid], xi[valid]] = 0.3
    #     if C>1:
    #         img[1, yi[valid], xi[valid]] = 0.
    #         img[2, yi[valid], xi[valid]] = 0.


def save_all(img:torch.Tensor, kpts:torch.Tensor, desc:torch.Tensor, sp_path:str, outimg_path:str, args):
    img = img[0]
    kpts = kpts[0]
    _ , H, W = img.shape
    score = torch.zeros((1, H, W), dtype=torch.float32)
    if args.mlp_method=="ALIKED" or args.mlp_method=="DISK":
        desc = F.interpolate(desc.unsqueeze(0),  size=(int(desc.shape[1]/8), int(desc.shape[2]/8)), 
                             mode='bilinear', align_corners=True).squeeze(0)
    plot_points(score, kpts)
    img = img.permute(1,2,0).cpu().numpy()
    if args.use_feature:
        torch.save(desc, f"{sp_path}_fmap.pt")
    torch.save(score, f"{sp_path}_smap.pt")
    score_vis = one_channel_vis(score)
    # score_vis.save(os.path.join(sp_path + "_smap_vis.png"))
    if outimg_path:
        img = img.copy()
        if img.dtype == np.float32 or img.dtype == np.float64:
            img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX)  # Normalize to 0-255
            img = img.astype(np.uint8)
        for x, y in kpts:
            cv2.circle(img, (int(x), int(y)), radius=1, color=(255, 0, 0), thickness=-1)
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        cv2.imwrite(f"{outimg_path}.color.png", img_rgb)
    

def main(args):
    if args.mlp_method=="DISK":
        conf = {
            "dense_outputs": True,
            "max_num_keypoints": int(args.max_num_keypoints),
        }
        model = DISK(conf).to("cuda").eval()
    elif args.mlp_method=="ALIKED":
        conf = {
            "model_name": "aliked-n16",
            "max_num_keypoints": int(args.max_num_keypoints),
            "detection_threshold": 0.
        }
        model = ALIKED(conf).to("cuda").eval()
    else:
        conf = {
            "sparse_outputs": True,
            "dense_outputs": True,
            "max_num_keypoints": int(args.max_num_keypoints),
            "detection_threshold": args.th,
        }
        model = SuperPoint(conf).to("cuda").eval()
    
    if args.mlp_method.startswith("SP"):
        mlp = get_mlp_model(dim = args.mlp_dim, type=args.mlp_method)
    elif args.mlp_method.startswith("dataset"):
        mlp = get_mlp_data_7scenes_Cambridege(dim=args.mlp_dim, dataset=args.mlp_method)
    elif args.mlp_method.startswith("all"):
        mlp = get_mlp_dataset(dim = args.mlp_dim, dataset=args.mlp_method)
    elif args.mlp_method.startswith("pgt") or args.mlp_method.startswith("pairs") or args.mlp_method.startswith("match"):
        mlp = get_mlp_dataset(dim=args.mlp_dim, dataset=args.mlp_method)
    elif args.mlp_method == "Cambridge":
        mlp = get_mlp_dataset(dim=args.mlp_dim, dataset=args.mlp_method)
    elif args.mlp_method.startswith("Cambridge"):
        mlp = get_mlp_dataset(dim=args.mlp_dim, dataset=args.mlp_method)
    elif args.mlp_method.startswith("augment"):
        mlp = get_mlp_augment(dim=args.mlp_dim, dataset=args.mlp_method)
    mlp = mlp.to("cuda").eval()
    img_folder = f"{args.source_path}/{args.images}"
    if args.output_images:
        ImgOut_folder = f"{args.source_path}/{args.output_images}"
        os.makedirs(ImgOut_folder, exist_ok=True)
    feature_folder = f"{args.source_path}/features/{args.feature_name}"
    target_images = [f for f in os.listdir(img_folder) if not os.path.isdir(os.path.join(img_folder, f))]
    target_images = [os.path.join(img_folder, f) for f in target_images]
    os.makedirs(feature_folder, exist_ok=True)

    for t in target_images:
        print(f"Processing '{t}'...")
        img_name = t.split(os.sep)[-1].split(".")[0]
        #############################################################
        # resize_num = int(args.resize_num)
        resize_num = 1
        #############################################################
        img_tensor = load_image2(t, resize=resize_num).to("cuda").unsqueeze(0)
        data = {}
        data["image"] = img_tensor
        pred = model(data)
        desc = pred["dense_descriptors"][0]
        desc_mlp = mlp(desc.permute(1,2,0)).permute(2,0,1).contiguous().cpu()
        kpts = pred["keypoints"].cpu()
        sp_path = f"{feature_folder}/{img_name}"
        if args.output_images:
            outimg_path = f"{ImgOut_folder}/{img_name}"
        save_all(img_tensor, kpts, desc_mlp, sp_path, outimg_path, args)


if __name__=="__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source_path", type=str,)
    parser.add_argument("--use_feature", action="store_true")
    parser.add_argument("--feat_name_op", type=str, default=None)
    parser.add_argument("--mlp_method", type=str, default="SP")
    parser.add_argument("--resize_num", type=int, default=1)
    parser.add_argument("--mlp_dim", type=int, default=16,)
    parser.add_argument("--th", type=float, default=0.01,)
    parser.add_argument("--max_num_keypoints", type=float, default=512,)
    parser.add_argument("--output_images", type=str, default=None)
    parser.add_argument("--images", type=str, default="rgb")
    args = parser.parse_args()
    feat_name = "twoPhase"
    args.feature_name = f"{feat_name}"
    main(args)
