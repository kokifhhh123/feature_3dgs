import cv2
import torch
import argparse
import os
from .superpoint import SuperPoint
from .utils import load_image, resize_image
import numpy as np
from .mlp import get_module_ckptpath





parser = argparse.ArgumentParser(description=("Get image embeddings of an input image or directory of images."))
parser.add_argument(
    "--input",
    type=str,
    required=True,
    help="Path to either a folder of images.",
)

parser.add_argument(
    "--output",
    type=str,
    required=True,
    help=(
        "Path to the directory where embeddings will be saved. Output will be either a folder "
        "of .pt per image or a single .pt representing image embeddings."
    ),
)

parser.add_argument("--device", type=str, default="cuda", help="The device to run generation on.")


from torchvision.transforms import ToPILImage
def saveimage_from_torch(image: torch, img_name = "image"):
    to_pil = ToPILImage()
    image_pil = to_pil(image)
    image_pil.save(f"{img_name}.png")






def main(args):
    print("Loading model...")
    model = SuperPoint({}).to(args.device)
    mlp, ckpt_path = get_module_ckptpath()
    mlp = mlp()
    ckpt = torch.load(ckpt_path)
    mlp.load_state_dict(ckpt)
    mlp.to(args.device)


    targets = [f for f in os.listdir(args.input) if not os.path.isdir(os.path.join(args.input, f))]
    targets = [os.path.join(args.input, f) for f in targets]

    os.makedirs(args.output, exist_ok=True)

    for t in targets:
        print(f"Processing '{t}'...")
        img_name = t.split(os.sep)[-1].split(".")[0]
        image = load_image(t).to(args.device)

        h, w = image.shape[1:]
        ratio = h/w
        size = [int(1024*ratio), 1024]
        image = resize_image(image, size)


        image = image.unsqueeze(0)
        
        if image is None:
            print(f"Could not load '{t}' as an image, skipping...")
            continue
        
        pred = model(image)
        desc = pred['descriptors'][0]
        scores = pred['keypoint_scores'][0]

        desc_mlp = mlp(desc.permute(1,2,0)).permute(2,0,1).contiguous()

        print(f'{img_name}: ')
        print("descriptors shape: ", desc_mlp.shape)
        print("scores shape: ", scores.shape)

        torch.save(desc_mlp, os.path.join(args.output, f"{img_name}_fmap_CxHxW.pt"))
        torch.save(scores, os.path.join(args.output, f"{img_name}_smap_CxH8xW8.pt"))








# python -m encoders.superpoint.extract_superpoint --input /home/koki/code/feature_3dgs/scene000000_B/images --output /home/koki/code/feature_3dgs/scene000000_B/superpoint_feature
if __name__=="__main__":
    args = parser.parse_args()
    main(args)

