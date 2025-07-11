import os
import json
import cv2
import numpy as np
from PIL import Image, ImageDraw
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, Subset
import torchvision.transforms.functional as TF
import random
import time
from tqdm import tqdm
from contour import mask_to_bezier_sequence, mask_to_vertex_sequence
import matplotlib.path as mpath # <<< NEW IMPORT
from torch.nn.utils.rnn import pad_sequence
# Ensure transformers is installed: pip install transformers
from transformers import CLIPProcessor, CLIPModel
import functools
import torch.nn.functional as F
from matplotlib.patches import Polygon as MplPolygon # Renamed to avoid clash
# Ensure scipy is installed: pip install scipy
from scipy.interpolate import splprep, splev
# Ensure shapely is installed: pip install Shapely
from shapely.geometry import Point, Polygon as ShapelyPolygon
from typing import Optional, List, Dict,Tuple # Kept Dict for return type hint
import traceback
import math
NUM_INTERIOR_POINTS = 256
CURVE_PARAMS = 6  # [cx1, cy1, cx2, cy2, ex, ey] - Model Output Format

import numpy as np
import math

def generate_deterministic_context_points(
    parent_verts_scaled: np.ndarray | None,
    parent_bin:          np.ndarray,
    N_total:             int,
    N_boundary:          int,
    W:                   int,
    H:                   int,
    sx:                  float,
    sy:                  float
) -> np.ndarray:
    """
    1) Take up to N_boundary points from the polygon boundary
       (first vertices, then evenly spaced along edges).
    2) If still need more (N_total - len(pts)), uniformly subsample
       that many points from the interior mask pixels.
    3) Never pad with [0,0].
    """
    pts = []

    # --- 1) Boundary sampling ---
    if N_boundary > 0 and parent_verts_scaled is not None and len(parent_verts_scaled) >= 2:
        verts = np.asarray(parent_verts_scaled, dtype=np.float32)
        # a) original vertices
        take_verts = min(len(verts), N_boundary)
        pts.extend(verts[:take_verts].tolist())

        # b) evenly‐spaced extras along edges
        extra = N_boundary - take_verts
        if extra > 0:
            # build closed loop
            edges = np.vstack([verts, verts[0]])
            vecs  = edges[1:] - edges[:-1]
            lens  = np.hypot(vecs[:,0], vecs[:,1])
            perim = lens.sum()
            dists = np.linspace(0, perim, extra, endpoint=False)
            cum   = np.concatenate([[0], np.cumsum(lens)])
            for d in dists:
                i = np.searchsorted(cum, d, side='right') - 1
                t = (d - cum[i]) / (lens[i] + 1e-12)
                p = verts[i] + t * vecs[i]
                pts.append([float(p[0]), float(p[1])])

    # --- 2) Interior sampling if still short ---
    needed = N_total - len(pts)
    if needed > 0 and parent_bin is not None:
        ys, xs = np.where(parent_bin > 0)
        M       = xs.shape[0]
        if M > 0:
            # uniformly subsample ‘needed’ indices from the sorted mask list
            idxs = np.linspace(0, M - 1, needed, dtype=int)
            sel  = np.stack([xs[idxs], ys[idxs]], axis=1).astype(np.float32)

            # normalize & scale
            sel[:,0] = (sel[:,0] / (W - 1 + 1e-9)) * sx
            sel[:,1] = (sel[:,1] / (H - 1 + 1e-9)) * sy

            pts.extend(sel.tolist())
        else:
            # no interior pixels: fall back to boundary verts if any
            if parent_verts_scaled is not None and len(parent_verts_scaled) > 0:
                pool = np.asarray(parent_verts_scaled, dtype=np.float32)
                idxs = np.arange(needed) % pool.shape[0]
                fallback = pool[idxs]
                pts.extend(fallback.tolist())
            else:
                # last resort: uniform grid over entire canvas
                side = int(np.ceil(np.sqrt(needed)))
                xs_lin = np.linspace(0, W-1, side)
                ys_lin = np.linspace(0, H-1, side)
                xx, yy = np.meshgrid(xs_lin, ys_lin, indexing='xy')
                grid   = np.stack([xx.ravel(), yy.ravel()], axis=1)[:needed]
                grid[:,0] = (grid[:,0] / (W - 1 + 1e-9)) * sx
                grid[:,1] = (grid[:,1] / (H - 1 + 1e-9)) * sy
                pts.extend(grid.tolist())

    # --- 3) Truncate if overshot ---
    if len(pts) > N_total:
        pts = pts[:N_total]

    return np.asarray(pts, dtype=np.float32)

# --- GPU function ---
def generate_deterministic_context_points_gpu(
    parent_verts_scaled: torch.Tensor | None,
    parent_bin:          torch.Tensor,
    N_total:             int,
    N_boundary:          int,
    N_interior:          int
) -> torch.Tensor:
    device = parent_bin.device
    dtype = torch.float32
    pts = []
    # 1) Boundary sampling: include each polygon vertex first
    if N_boundary > 0 and parent_verts_scaled is not None and parent_verts_scaled.size(0) >= 1:
        V = parent_verts_scaled.to(device=device, dtype=dtype)
        # Add each vertex
        pts.extend(V.cpu().tolist())
        extra = N_boundary - V.size(0)
        if extra > 0:
            # Distribute extra points along edges
            edges = torch.cat([V, V[0:1]], dim=0)
            vecs = edges[1:] - edges[:-1]
            lens = vecs.norm(dim=1)
            perim = lens.sum()
            dists = torch.linspace(0.0, perim, steps=extra, device=device, dtype=dtype)
            cum = torch.cat([torch.zeros(1,device=device,dtype=dtype), lens.cumsum(dim=0)])
            idx = (torch.bucketize(dists, cum) - 1).clamp(0, lens.size(0)-1)
            t = (dists - cum[idx]) / (lens[idx] + 1e-12)
            extra_pts = (V[idx] + vecs[idx] * t.unsqueeze(1)).cpu().tolist()
            pts.extend(extra_pts)
    else:
        pts.extend([[0.0, 0.0]] * N_boundary)

    # 2) Interior via uniform grid
    if N_interior > 0 and parent_bin is not None:
        H, W = parent_bin.shape
        g = int(np.ceil(np.sqrt(N_interior)))
        xs = torch.linspace(0, W-1, steps=g, device=device)
        ys = torch.linspace(0, H-1, steps=g, device=device)
        xx, yy = torch.meshgrid(xs, ys, indexing='xy')
        cand = torch.stack([xx.reshape(-1), yy.reshape(-1)], dim=1)
        ix, jx = cand[:,1].long(), cand[:,0].long()
        inside = parent_bin[ix, jx] > 0
        valid = cand[inside]
        Mv = valid.size(0)
        if Mv >= N_interior:
            sel = valid[:N_interior]
        else:
            pad = valid[-1:].expand(N_interior - Mv, 2)
            sel = torch.cat([valid, pad], dim=0)
        sel = sel.cpu().numpy()
        sel[:,0] = sel[:,0] / (W - 1 + 1e-9)
        sel[:,1] = sel[:,1] / (H - 1 + 1e-9)
        pts_i = sel.tolist()
    else:
        pts_i = [[0.0, 0.0]] * N_interior
    pts.extend(pts_i)

    # 3) Pad/truncate
    if len(pts) < N_total:
        pts.extend([pts[-1]] * (N_total - len(pts)))
    pts = pts[:N_total]
    return torch.tensor(pts, dtype=torch.float32, device=device)

def load_mask_np(mask_path):
    img = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
    _, binary = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)
    return binary


def extract_full_contour(mask_np):
    contours, _ = cv2.findContours(mask_np, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    contour = max(contours, key=cv2.contourArea)
    return contour.squeeze(1)


def simplify_contour(contour, epsilon_ratio):
    epsilon = epsilon_ratio * cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, epsilon, True)
    return approx.squeeze(1)


def mask_to_spline_points(mask_np, W, H, num_samples, smoothing):
    """
    Fits a periodic smoothing spline to the detailed full contour of the mask,
    then samples `num_samples` points equidistant in parameter space.
    Normalizes coordinates to [0,1].
    """
    contour = extract_full_contour(mask_np)
    x = contour[:, 0].astype(np.float32)
    y = contour[:, 1].astype(np.float32)
    # Fit periodic spline
    try:
        tck, _ = splprep([x, y], s=smoothing, per=True)
        u_new = np.linspace(0, 1, num_samples)
        x_new, y_new = splev(u_new, tck)
    except Exception:
        # Fallback: uniform sampling of raw contour
        total = len(x)
        idx = np.linspace(0, total - 1, num_samples).astype(int)
        x_new = x[idx]
        y_new = y[idx]
    pts = np.vstack([x_new / W, y_new / H]).T
    return pts


from scipy.optimize import minimize

# --- (1) Bézier fitting helper ---
def fit_quadratic_bezier(points):
    p0, p2 = points[0], points[-1]
    def loss(p1_flat):
        p1 = p1_flat.reshape(2)
        t = np.linspace(0, 1, len(points))[:, None]
        curve = (1 - t)**2 * p0 + 2*(1 - t)*t*p1 + t**2*p2
        return np.sum(np.linalg.norm(curve - points, axis=1))
    init = (p0 + p2) / 2
    res = minimize(loss, init, method='Powell')
    return p0, res.x, p2

# --- (2) Complexity & deviation metrics ---
def measure_complexity(segment):
    chord = np.linalg.norm(segment[-1] - segment[0])
    arc   = np.sum(np.linalg.norm(np.diff(segment, axis=0), axis=1))
    return arc / chord if chord>1e-8 else 1.0

def find_max_deviation_point(segment):
    p0, p1 = segment[0], segment[-1]
    chord = p1 - p0
    L = np.linalg.norm(chord)
    if L<1e-8: return None, 0.0
    unit = chord / L
    normal = np.array([-unit[1], unit[0]])
    devs = [abs(np.dot((pt-p0), normal)) for pt in segment]
    idx = int(np.argmax(devs))
    return idx, devs[idx]

# --- (3) Recursive splitter & fitter ---
def split_and_fit(segment, threshold_complex=1.01, threshold_dev=.5):
    """Return a list of (p0, ctrl, p2, type_flag) for this segment."""
    # Base straight‐line check
    comp = measure_complexity(segment)
    if comp <= threshold_complex:
        # too straight → one line
        p0, p2 = segment[0], segment[-1]
        return [ (p0, p0, p2, 0) ]  # type=0 for straight

    # find max deviation
    idx, dev = find_max_deviation_point(segment)
    if dev <= threshold_dev or idx in (0, len(segment)-1):
        # not enough deviation to warrant split → fit one quadratic
        a,b,c = fit_quadratic_bezier(segment)
        return [ (a, b, c, 1) ]  # type=1 for curve

    # otherwise, split at idx and recurse
    first  = split_and_fit(segment[:idx+1], threshold_complex, threshold_dev)
    second = split_and_fit(segment[idx:],   threshold_complex, threshold_dev)
    return first + second


def check_context_point_validity(context_points, parent_bin, sx, sy):
    """
    Checks which context points fall inside the binary parent mask.

    Args:
        context_points (Tensor): [N, 2] in scaled space
        parent_bin (ndarray): [H, W] binary mask
        sx, sy (float): scaling factors for x and y
    Returns:
        validity_mask (ndarray): [N] boolean array indicating validity
    """
    H, W = parent_bin.shape
    points = context_points.clone().detach().cpu().numpy()
    xs = np.clip((points[:, 0] / sx * W).astype(int), 0, W - 1)
    ys = np.clip((points[:, 1] / sy * H).astype(int), 0, H - 1)
    mask_values = parent_bin[ys, xs] > 0
    return mask_values  # boolean array of length N

def visualize_context_with_mask(context_points, parent_bin, sx, sy):
    validity = check_context_point_validity(context_points, parent_bin, sx, sy)
    points = context_points.clone().detach().cpu().numpy()

    plt.imshow(parent_bin, cmap='gray')
    plt.scatter(points[:,0] / sx * parent_bin.shape[1],
                points[:,1] / sy * parent_bin.shape[0],
                c=['green' if v else 'red' for v in validity], s=4)
    plt.title("Green = Inside, Red = Outside")
    plt.show()

def save_visualization(pts, mismatches, mask_path, out_path="mask_sampling_debug.png"):
    """
    pts:       (N,2) numpy array of sampled points in [0,1]×[0,1]
    mismatches: list of tuples (idx, [x,y], pixel_in:bool, poly_in:bool)
    mask_path: string path (for title)
    out_path:  filename to save the PNG
    """
    # extract only the truly disagreeing points
    wrong_pts = np.array([pt for (_i, pt, pix, poly) in mismatches if pix != poly],
                         dtype=np.float32)

    fig, ax = plt.subplots(figsize=(6,6))
    ax.set_title(f"Sampling on {mask_path}", fontsize=10)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    # plot all points in light gray
    ax.scatter(pts[:,0], pts[:,1],
               s=5, c="lightgray", label="all points")

    # overlay the mismatches in red X
    if wrong_pts.size:
        ax.scatter(wrong_pts[:,0], wrong_pts[:,1],
                   s=30, c="red", marker="x", label="pixel≠poly")

    ax.legend(markerscale=2, fontsize=8)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"Saved visualization to {out_path}")
import os
import json
from typing import Optional

import cv2
import numpy as np
from PIL import Image
from torch.utils.data import Dataset
import torch
from tqdm import tqdm

from contour import mask_to_bezier_sequence
from transformers import AutoTokenizer, T5EncoderModel


class AugmentedDataset(Dataset):
    """
    Loads scenes, computes:
      - FLAN-T5 sequence embeddings for child & parent (fixed [512, hidden])
      - parent_bbox corners [4,2]
      - normalized parent Bézier segments
      - normalized child Bézier GT curves
    """
    def __init__(
        self,
        root_dir: str = "dataset",
        json_dir: str = "json",
        masks_dir: str = "masks",
        images_dir: str = "images",
        max_samples: int = None,
        poly_epsilon_ratio: float = 0.01,
        text_max_length: int = 512,
    ):
        super().__init__()
        # FLAN-T5 setup for sequence embeddings
        self.tokenizer = AutoTokenizer.from_pretrained("google/flan-t5-base")
        self.encoder = T5EncoderModel.from_pretrained("google/flan-t5-base").encoder.eval()
        for p in self.encoder.parameters(): p.requires_grad = False
        self.text_max_length = text_max_length
        self.hidden_size = self.encoder.config.hidden_size
        self.epsilon = float(poly_epsilon_ratio)

        # Paths
        self.json_dir = os.path.join(root_dir, json_dir)
        self.masks_dir = os.path.join(root_dir, masks_dir)
        self.images_dir = os.path.join(root_dir, images_dir)

        # Load JSON metadata
        scene_to_shapes = {}
        for fn in os.listdir(self.json_dir):
            if not fn.endswith('.json'): continue
            data = json.load(open(os.path.join(self.json_dir, fn)))
            scene = fn[:-5]
            if isinstance(data.get('scene'), list):
                scene_to_shapes[scene] = data['scene']

        # Gather raw entries
        raw = []
        available = {f[:-4] for f in os.listdir(self.images_dir) if f.endswith('.png')}
        for scene, shapes in scene_to_shapes.items():
            if scene not in available: continue
            id_map = {s['id']: s for s in shapes if isinstance(s, dict) and 'id' in s}
            for s in shapes:
                if not isinstance(s, dict): continue
                pid, mname = s.get('parent'), s.get('mask_path')
                if pid is None or pid < 0 or not mname: continue
                child_mask = os.path.join(self.masks_dir, scene, mname)
                if not os.path.exists(child_mask): continue
                parent_s = id_map.get(pid, {})
                pm = parent_s.get('mask_path')
                if pm and os.path.exists(os.path.join(self.masks_dir, scene, pm)):
                    parent_mask = os.path.join(self.masks_dir, scene, pm)
                    parent_is_image = False
                else:
                    parent_mask = os.path.join(self.images_dir, f"{scene}.png")
                    parent_is_image = True
                raw.append({
                    'scene': scene,
                    'child_mask': child_mask,
                    'parent_mask': parent_mask,
                    'parent_is_image': parent_is_image,
                    'child_desc': s.get('description', ''),
                    'parent_desc': parent_s.get('description', '')
                })
                if max_samples and len(raw) >= max_samples:
                    break
            if max_samples and len(raw) >= max_samples:
                break

        # Precompute all samples
        self.samples = []
        for info in raw:
            # load image for bbox
            img = Image.open(os.path.join(self.images_dir, f"{info['scene']}.png"))
            W, H = img.size; canvas = max(W, H)
            # child mask
            child_bin = cv2.resize(
                cv2.imread(info['child_mask'], cv2.IMREAD_GRAYSCALE),
                (W, H), interpolation=cv2.INTER_NEAREST
            )
            # parent mask or white
            if not info['parent_is_image']:
                pm = cv2.resize(
                    cv2.imread(info['parent_mask'], cv2.IMREAD_GRAYSCALE),
                    (W, H), interpolation=cv2.INTER_NEAREST
                )
            else:
                pm = np.ones((H, W), np.uint8) * 255
            # letterbox
            mask_sq = np.zeros((canvas, canvas), np.uint8)
            mask_sq[:H, :W] = pm
            # bbox
            ys, xs = np.where(mask_sq > 0)
            if xs.size>0:
                x_min, x_max = xs.min(), xs.max()
                y_min, y_max = ys.min(), ys.max()
            else:
                x_min, y_min, x_max, y_max = 0,0,canvas-1,canvas-1
            bbox = torch.tensor([
                [x_min,y_min], [x_min,y_max],
                [x_max,y_min], [x_max,y_max]
            ], dtype=torch.float32) / float(canvas - 1)
            # parent bezier
            _, p_segs_raw = mask_to_bezier_sequence(
                mask_sq, max_ctrl=2, dev_thresh=0,
                epsilon_ratio=self.epsilon, merge_thresh=0.01, angle_thresh_deg=1
            )
            p_segs = torch.tensor(p_segs_raw, dtype=torch.float32)
            p_segs[p_segs>=0] /= float(canvas - 1)
            pad_len = 30 - p_segs.size(0)
            pad_tensor = -1 * torch.ones((pad_len, 6), dtype=p_segs.dtype, device=p_segs.device)
            p_segs= torch.cat([p_segs, pad_tensor], dim=0)
            # child GT
            mask_sq[:,:] = 0; mask_sq[:H,:W] = child_bin
            _, c_segs_raw = mask_to_bezier_sequence(
                mask_sq, max_ctrl=2, dev_thresh=0,
                epsilon_ratio=self.epsilon, merge_thresh=0.01, angle_thresh_deg=1
            )
            gt = torch.tensor(c_segs_raw, dtype=torch.float32)
            gt[gt>=0] /= float(canvas - 1)
            lengths = gt.size(0)
            pad_len = 30 - gt.size(0)
            pad_tensor = -1 * torch.ones((pad_len, 6), dtype=gt.dtype, device=gt.device)
            gt = torch.cat([gt, pad_tensor], dim=0)
            # text embeddings (sequence)
            toks_c = self.tokenizer(
                [info['child_desc']],
                padding='max_length', truncation=True,
                max_length=self.text_max_length,
                return_tensors='pt'
            )
            toks_p = self.tokenizer(
                [info['parent_desc']],
                padding='max_length', truncation=True,
                max_length=self.text_max_length,
                return_tensors='pt'
            )
            # encode
            with torch.no_grad():
                enc_c = self.encoder(
                    input_ids=toks_c.input_ids,
                    attention_mask=toks_c.attention_mask
                )
                enc_p = self.encoder(
                    input_ids=toks_p.input_ids,
                    attention_mask=toks_p.attention_mask
                )
            seq_c = enc_c.last_hidden_state.squeeze(0)  # [512, H]
            seq_p = enc_p.last_hidden_state.squeeze(0)
            mask_c = toks_c.attention_mask.squeeze(0).bool()  # [512]
            mask_p = toks_p.attention_mask.squeeze(0).bool()
            # store
            self.samples.append({
                'child_embs': seq_c,
                'child_mask': mask_c,
                'parent_embs': seq_p,
                'parent_mask': mask_p,
                'parent_bbox': bbox,
                'parent_bezier': p_segs,
                'gt_curves': gt,
                'lengths': lengths
            })

    def __len__(self): return len(self.samples)
    
    def __getitem__(self, idx):
        sample = self.samples[idx]
        p_segs = sample['parent_bezier']
        # for a single sample, there's no padding, so mask is all False
        pad_mask = torch.zeros(p_segs.size(0), dtype=torch.bool)

        return {
            'child_embs':        sample['child_embs'],
            'child_mask':        sample['child_mask'],
            'parent_embs':       sample['parent_embs'],
            'parent_mask':       sample['parent_mask'],
            'parent_bbox':       sample['parent_bbox'],
            'parent_bezier':     sample['parent_bezier'],
            'parent_bezier_segs':sample['parent_bezier'],   # same as parent_bezier
            'padding_mask':      pad_mask,
            'gt_curves':         sample['gt_curves'],
            'lengths':           sample['lengths'],
        }


# -----------------------------------------------------------------------------
# collate_fn
# -----------------------------------------------------------------------------
def collate_fn(batch):
    child_embs    = torch.stack([b['child_embs']    for b in batch], dim=0)
    child_mask    = torch.stack([b['child_mask']    for b in batch], dim=0)
    parent_embs   = torch.stack([b['parent_embs']   for b in batch], dim=0)
    parent_mask   = torch.stack([b['parent_mask']   for b in batch], dim=0)
    bbox          = torch.stack([b['parent_bbox']   for b in batch], dim=0)

    # pad parent_bezier (and use same for parent_bezier_segs)
    pb              = [b['parent_bezier'] for b in batch]
    parent_bezier   = torch.nn.utils.rnn.pad_sequence(pb, batch_first=True, padding_value=-1.0)
    padding_mask    = parent_bezier[:,:,0] < 0

    # pad gt_curves
    gc           = [b['gt_curves'] for b in batch]
    gt_curves    = torch.nn.utils.rnn.pad_sequence(gc, batch_first=True, padding_value=0.0)

    lengths      = torch.tensor([b['lengths'] for b in batch], dtype=torch.long)

    return {
        'child_embs':        child_embs,
        'child_mask':        child_mask,
        'parent_embs':       parent_embs,
        'parent_mask':       parent_mask,
        'parent_bbox':       bbox,
        'parent_bezier':     parent_bezier,
        'parent_bezier_segs':parent_bezier,   # same padded tensor
        'padding_mask':      padding_mask,
        'gt_curves':         gt_curves,
        'lengths':           lengths,
    }

from transformers import T5Tokenizer, T5EncoderModel
from dataclasses import dataclass

@dataclass
class PolygonConfig:
    d_model: int = 128
    n_head: int = 8
    # num_enc_layers_shape: int = 4 # Kept for potential future use with a Transformer parent encoder
    num_dec_layers_seq: int = 6   # For ShapePredictor (decoder)
    dim_feedforward: int = 512
    max_segments: int = 30        # For output child shapes (T)
    dropout: float = 0.1
    num_fusion_layers: int = 2
    t5_model_name: str = "google/flan-t5-base"
    max_text_length: int = 512

class PositionalEncoding(nn.Module):
    def __init__(self, d_model, dropout=0.1, max_len=30):
        super().__init__()
        self.dropout = nn.Dropout(dropout)
        pe = torch.zeros(max_len, d_model)
        pos = torch.arange(max_len).unsqueeze(1).float()
        div = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        
        pe[:, 0::2] = torch.sin(pos * div)
        pe[:, 1::2] = torch.cos(pos * div)
        self.register_buffer('pe', pe.unsqueeze(0))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.pe[:, : x.size(1)]
        return self.dropout(x) 

def generate_square_subsequent_mask(sz: int) -> torch.Tensor:
    mask = torch.triu(torch.ones(sz, sz, dtype=torch.bool), diagonal=1)
    return mask.float().masked_fill(mask, float('-inf')).masked_fill(~mask, 0.0)

class ShapePredictor(nn.Module):
    def __init__(self,
                 d_model: int,
                 num_heads: int,
                 num_decoder_layers: int,
                 num_segments: int,
                 dim_feedforward: int,
                 dropout: float,
                 out_dim: int = 6):
        super().__init__()
        self.num_segments = num_segments
        self.d_model = d_model

        # Revert query_embed to standard initialization (important for PE)
        self.query_embed = nn.Parameter(torch.randn(num_segments, d_model))

        self.positional_encoder = PositionalEncoding(d_model, dropout, max_len=num_segments)

        decoder_layer = nn.TransformerDecoderLayer(
            d_model=d_model,
            nhead=num_heads,
            dim_feedforward=dim_feedforward,
            dropout=dropout,
            batch_first=True,
            activation=F.gelu # Standard for Transformer FFN
        )
        self.decoder = nn.TransformerDecoder(
            decoder_layer, num_layers=num_decoder_layers,
            norm=nn.LayerNorm(d_model)
        )

        # Output head with SquareActivation
        self.output_head = nn.Sequential(
            nn.Linear(d_model, d_model),  # L1
            nn.ReLU(),
            nn.Linear(d_model, out_dim),   # L2
            nn.Sigmoid()
        )
        # Optional: Initialize bias of L1 if needed, but square activation is less prone to "dying"
        # if hasattr(self.output_head[0], 'bias') and self.output_head[0].bias is not None:
        #     nn.init.constant_(self.output_head[0].bias.data, 0.0) # Or small positive

        # Optional: If after SquareActivation, L2 still produces small variance,
        # you could apply the custom weight initialization to self.output_head[2] discussed before.
        # However, SquareActivation might make this unnecessary by providing larger variance input to L2.



    def forward(self, H_memory: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        B = H_memory.size(0)
        content_queries = self.query_embed.unsqueeze(0).expand(B, -1, -1)
        tgt = self.positional_encoder(content_queries)
        print("tgt mean:", tgt.mean().item(), "std:", tgt.std().item())  # Debugging line
        decoded_hidden_states = self.decoder(tgt=tgt, memory=H_memory)
        print("decoded_hidden_states mean:", decoded_hidden_states.mean().item(), "std:", decoded_hidden_states.std().item())  # Debugging line
        # DEBUG: Check scale
        # print(f"decoded_hidden_states mean: {decoded_hidden_states.mean().item():.4f}, std: {decoded_hidden_states.std().item():.4f}")
        # L1_output = self.output_head[0](decoded_hidden_states)
        # print(f"L1_output (before Square) mean: {L1_output.mean().item():.4f}, std: {L1_output.std().item():.4f}")
        # Square_output = self.output_head[1](L1_output)
        # print(f"Square_output mean: {Square_output.mean().item():.4f}, std: {Square_output.std().item():.4f}")

        coords = self.output_head(decoded_hidden_states)
        # Your existing print for final coords before sigmoid:
        end_coords = coords[..., 5:6]
        print("coords mean:", end_coords.mean().item(), "std:", end_coords.std().item()) 
        
        return coords, decoded_hidden_states

class SimpleShapeEncoder(nn.Module): # Parent Shape Encoder
    def __init__(self, in_dim: int = 6, dim: int = 128, seq_len: int = 50):
        super().__init__()
        self.seq_len = seq_len
        self.pos_emb = nn.Parameter(torch.randn(seq_len, dim))
        self.input_proj = nn.Linear(in_dim, dim)
        self.mlp = nn.Sequential(
            nn.Linear(dim, dim),
            nn.ReLU(),
            nn.Linear(dim, dim)
        )
        # self.pool = nn.AdaptiveAvgPool1d(1) # We will do masked mean manually
        self.to_latent = nn.Linear(dim, dim)

    def forward(self, shape_pts: torch.Tensor) -> torch.Tensor:
        B, S_parent_actual, C_in = shape_pts.shape

        if C_in != self.input_proj.in_features:
            raise ValueError(f"SimpleShapeEncoder expects {self.input_proj.in_features} features, got {C_in}")
        if S_parent_actual > self.seq_len:
            shape_pts = shape_pts[:, :self.seq_len, :]
            S_parent_actual = self.seq_len

        x = self.input_proj(shape_pts)
        x = x + self.pos_emb[:S_parent_actual, :].unsqueeze(0)

        # Derive valid_segment_mask (True for valid segments) based on -1 fill convention
        is_padding_segment = (shape_pts == -1).all(dim=2) # (B, S_parent_actual)
        actual_lengths = torch.full((B,), S_parent_actual, dtype=torch.long, device=shape_pts.device)
        for i in range(B):
            first_pad_idx = torch.where(is_padding_segment[i])[0]
            if len(first_pad_idx) > 0:
                actual_lengths[i] = first_pad_idx[0]
        
        s_indices = torch.arange(S_parent_actual, device=x.device).expand(B, S_parent_actual)
        # valid_segment_mask: (B, S_parent_actual), True for valid segments
        valid_segment_mask_2D = (s_indices < actual_lengths.unsqueeze(1))
        
        # Apply mask before MLP to zero out padded embeddings
        x = x * valid_segment_mask_2D.float().unsqueeze(-1) # (B, S_parent_actual, 1)

        x = self.mlp(x)
        # Re-apply mask after MLP in case MLP made padded parts non-zero
        x = x * valid_segment_mask_2D.float().unsqueeze(-1)

        # Masked average pooling:
        summed_features = x.sum(dim=1)  # Summing along the sequence dimension
        num_active_elements = actual_lengths.float().unsqueeze(-1).clamp(min=1.0) # Use actual lengths
        x_pooled = summed_features / num_active_elements
            
        return self.to_latent(x_pooled)


class PolygonPredictor(nn.Module):
    def __init__(self, cfg: PolygonConfig):
        super().__init__()
        self.cfg = cfg

        try:
            from transformers import AutoTokenizer, T5EncoderModel
            self.tokenizer = AutoTokenizer.from_pretrained(cfg.t5_model_name)
            full_t5_model = T5EncoderModel.from_pretrained(cfg.t5_model_name)
            self.encoder = full_t5_model.encoder
            d_text = full_t5_model.config.hidden_size
            for p in self.encoder.parameters():
                p.requires_grad = False
        except ImportError:
            print(f"Warning: transformers library not found. Mocking T5 components.")
            self.tokenizer = None; self.encoder = nn.Identity(); d_text = 768
        except Exception as e:
            print(f"Error loading T5 model '{cfg.t5_model_name}': {e}. Mocking T5 components.")
            self.tokenizer = None; self.encoder = nn.Identity(); d_text = 768

        self.text_proj = nn.Linear(d_text, cfg.d_model)

        # Parent Shape Encoder
        self.shape_encoder = SimpleShapeEncoder(
            in_dim=6,
            dim=cfg.d_model, # hidden_dim and latent_dim are cfg.d_model
            seq_len=cfg.max_segments
        )

        # Fusion module - now for 3 modalities if b_feat is removed.
        # child_text, parent_text, parent_shape_feature
        self.modality_type_embeddings = nn.Parameter(torch.randn(3, cfg.d_model))
        fusion_layer = nn.TransformerEncoderLayer(
            d_model=cfg.d_model, nhead=cfg.n_head,
            dim_feedforward=cfg.dim_feedforward, dropout=cfg.dropout,
            batch_first=True, activation=F.gelu
        )
        self.fusion_enc = nn.TransformerEncoder(
            fusion_layer, num_layers=cfg.num_fusion_layers,
            norm=nn.LayerNorm(cfg.d_model)
        )

        # Decoder and other heads
        self.coord_decoder = ShapePredictor(
            d_model=cfg.d_model,
            num_heads=cfg.n_head,
            num_decoder_layers=cfg.num_dec_layers_seq,
            num_segments=cfg.max_segments, # CRITICAL: Aligned with child output
            dim_feedforward=cfg.dim_feedforward,
            dropout=cfg.dropout,
            out_dim=6
        )
        self.type_head = nn.Linear(cfg.d_model, 3)
        self.stop_head = nn.Sequential(
            nn.Linear(cfg.d_model, cfg.max_segments),
            nn.Sigmoid()  # Outputs probabilities for stop decision per segment
        )


    def encode_text_embeddings(self, text_embeddings: torch.Tensor) -> torch.Tensor:
        # Processes (B, S_text, D_t5) or (B, D_t5) into (B, cfg.d_model)
        if text_embeddings.dim() == 3:
            # This simple mean pooling assumes no padding or that padding tokens are zero
            # For more robust pooling with padding, an attention mask from tokenizer output would be needed here.
            pooled_embs = text_embeddings.mean(dim=1)
        elif text_embeddings.dim() == 2:
            pooled_embs = text_embeddings
        else:
            raise ValueError(f"Unexpected text_embeddings dim: {text_embeddings.shape}")
        return self.text_proj(pooled_embs)

    def encode_parent_shape(self, parent_bezier_data: torch.Tensor) -> torch.Tensor:
        return self.shape_encoder(parent_bezier_data)

    def fuse_modalities(self, c_feat, p_feat, s_feat) -> torch.Tensor: # b_feat removed
        # All inputs should be (B, cfg.d_model)
        mods = torch.stack([c_feat, p_feat, s_feat], dim=1) # Stack 3 features
        if mods.size(1) != self.modality_type_embeddings.size(0):
             raise ValueError(f"Number of stacked modalities ({mods.size(1)}) != number of modality embeddings ({self.modality_type_embeddings.size(0)})")
        mods = mods + self.modality_type_embeddings.unsqueeze(0)
        fused_memory = self.fusion_enc(mods) # (B, 3, cfg.d_model)
        return fused_memory

    def decode_outputs_from_memory(self, fused_mem: torch.Tensor) -> Dict[str, torch.Tensor]:
        coords_normalized, decoded_hidden_states = self.coord_decoder(fused_mem)
        types_logits = self.type_head(decoded_hidden_states)
        stop_logits_per_step = self.stop_head(decoded_hidden_states)
        stop_scores = stop_logits_per_step.mean(dim=1)
        stop_index = stop_scores.argmax(dim=1)

        return {
            "coords_normalized": coords_normalized,
            "types_logits": types_logits,
            "stop_index": stop_index,
            "stop_scores": stop_scores,  # Optional, if needed for further analysis
        }

    def forward(self,
                child_embs: torch.Tensor,    # (B, S_text, D_t5) or (B, D_t5)
                parent_embs: torch.Tensor,   # (B, S_text, D_t5) or (B, D_t5)
                parent_bezier: torch.Tensor, # (B, cfg.max_segments, 6)
               ) -> Dict[str, torch.Tensor]:

        device = parent_bezier.device # A tensor that will surely be present
        child_embs = child_embs.to(device)
        parent_embs = parent_embs.to(device)

        # 1. Encode Inputs
        c_feat = self.encode_text_embeddings(child_embs)
        p_feat = self.encode_text_embeddings(parent_embs)
        s_feat = self.encode_parent_shape(parent_bezier)

        # 2. Fuse Modalities (b_feat is excluded as child is relative to parent)
        mem = self.fuse_modalities(c_feat, p_feat, s_feat)

        # 3. Decode child shape attributes
        decoder_outputs = self.decode_outputs_from_memory(mem)
        pred_coords_normalized = decoder_outputs["coords_normalized"] # (B, cfg.max_segments, 6)

        # 4. Scale normalized coordinates relative to the PARENT shape's bounding box
        # Derive parent_bbox from parent_bezier (assuming parent_bezier coords are absolute)
        # parent_bezier is (B, max_segments, 6). Reshape to access points.
        parent_pts_reshaped = parent_bezier.reshape(parent_bezier.size(0), -1, 2) # (B, max_p_seg * 3, 2)
        
        mask = parent_pts_reshaped[..., 0] != -1  # Assumes both x and y are -1 together

        # For X coordinates
        x_vals = parent_pts_reshaped[..., 0]
        x_masked_min = x_vals.masked_fill(~mask, float('inf'))
        x_masked_max = x_vals.masked_fill(~mask, float('-inf'))
        parent_xmin, _ = x_masked_min.min(dim=1, keepdim=True)
        parent_xmax, _ = x_masked_max.max(dim=1, keepdim=True)

        # For Y coordinates
        y_vals = parent_pts_reshaped[..., 1]
        y_masked_min = y_vals.masked_fill(~mask, float('inf'))
        y_masked_max = y_vals.masked_fill(~mask, float('-inf'))
        parent_ymin, _ = y_masked_min.min(dim=1, keepdim=True)
        parent_ymax, _ = y_masked_max.max(dim=1, keepdim=True)

        parent_mins = torch.cat([parent_xmin, parent_ymin], dim=1).unsqueeze(1) # (B, 1, 2)
        parent_ranges = torch.cat([parent_xmax - parent_xmin, parent_ymax - parent_ymin], dim=1).unsqueeze(1).clamp(min=1e-6) # (B, 1, 2)

        # Scale pred_coords_normalized (0-1) using parent's bounding box
        child_pts_reshaped = pred_coords_normalized.reshape(pred_coords_normalized.size(0), -1, 2)
        scaled_child_pts = child_pts_reshaped * parent_ranges + parent_mins
        scaled_coords = scaled_child_pts.reshape(pred_coords_normalized.shape)

        # 5. Apply post-processing
        final_pred_coords = scaled_coords.clone()
        B, T_seq, C_coord = final_pred_coords.shape

        pred_types = torch.argmax(decoder_outputs["types_logits"], dim=-1)
        col_indices_coord = torch.arange(C_coord, device=final_pred_coords.device)

        mask_type0 = (pred_types == 0)
        if mask_type0.any():
            col_mask_4 = (col_indices_coord < 4)
            effective_mask_type0 = mask_type0.unsqueeze(-1) & col_mask_4.view(1, 1, -1)
            final_pred_coords.masked_fill_(effective_mask_type0, -1.0)

        mask_type1 = (pred_types == 1)
        if mask_type1.any():
            col_mask_2 = (col_indices_coord < 2)
            effective_mask_type1 = mask_type1.unsqueeze(-1) & col_mask_2.view(1, 1, -1)
            final_pred_coords.masked_fill_(effective_mask_type1, -1.0)

        stop_indices = decoder_outputs["stop_index"]
        indices_seq_2D = torch.arange(T_seq, device=final_pred_coords.device).unsqueeze(0)
        mask_after_stop_2D = indices_seq_2D > stop_indices.unsqueeze(1)

        if mask_after_stop_2D.any():
            final_pred_coords[mask_after_stop_2D] = -1.0
        # print(f"Final pred coords shape: {final_pred_coords}, ")
        return {
            "segments": final_pred_coords,
            "type_logits": decoder_outputs["types_logits"],
            "stops": stop_indices,
            "stop_scores": decoder_outputs["stop_scores"],
        }


import torch
import torch.nn.functional as F

# def train_batch_shape_only(
#     model,
#     batch,
#     optimizer,
#     device,
#     batch_idx,
#     lambda_shape: float = 20000.0,
#     debug_mode:   bool  = True,
# ):
#     """
#     A variant of train_batch that ignores all losses except the shape (sampled Bézier) loss.
#     """
#     # 1) Move batch to device
#     child_embs   = batch['child_embs'].to(device)           # [B, E]
#     parent_embs  = batch['parent_embs'].to(device)          # [B, E]
#     parent_bbox  = batch['parent_bbox'].to(device)          # [B,4,2]
#     parent_segs  = batch['parent_bezier_segs'].to(device)   # [B, T_par, 6]
#     gt_curves    = batch['gt_curves'].to(device)            # [B, T_gt, 6]
#     lengths      = batch['lengths'].to(device)              # [B]
#     B            = gt_curves.size(0)

#     # 2) Forward pass (no teacher forcing)
#     outputs = model(
#         child_embs,
#         parent_embs,
#         parent_bbox,
#         parent_segs,
#         teacher_forcing=False,
#         gt_curves=gt_curves
#     )
#     pred = outputs['segments']  # [B, T_pred, 6]

#     # 3) Align time-steps
#     T_pred = pred.size(1)
#     T_gt   = gt_curves.size(1)
#     T      = min(T_pred, T_gt)
#     pred   = pred[:, :T, :]     # [B, T, 6]
#     gt     = gt_curves[:, :T, :] # [B, T, 6]

#     # 4) Build valid time‐mask [B, T]
#     valid_mask = (torch.arange(T, device=device)[None, :] < lengths[:, None]).float()

#     # 5) Sample Bézier points (auto‐detect type via -1 in controls)
#     def sample_bezier_batch(starts, curves, steps=50):
#         B_, T_ = starts.shape[:2]
#         t      = torch.linspace(0,1,steps,device=device).view(1,1,steps,1)
#         p0     = starts.view(B_,T_,1,2)
#         p1     = curves[...,0:2].view(B_,T_,1,2)
#         p2     = curves[...,2:4].view(B_,T_,1,2)
#         p3     = curves[...,4:6].view(B_,T_,1,2)
#         line   = (1-t)*p0    + t*p3
#         quad   = (1-t)**2*p0 + 2*(1-t)*t*p2 + t**2*p3
#         cubic  = (1-t)**3*p0 + 3*(1-t)**2*t*p1 + 3*(1-t)*t**2*p2 + t**3*p3
#         m1     = (curves[...,0:2]<0).all(dim=-1).view(B_,T_,1,1)
#         m2     = (curves[...,2:4]<0).all(dim=-1).view(B_,T_,1,1)
#         return torch.where(m1&m2, line, torch.where(m1, quad, cubic))

#     # 6) Compute start‐points for sampling
#     gt_ends   = gt[..., 4:6]
#     pred_ends = pred[..., 4:6]
#     gt_st     = torch.roll(gt_ends,   shifts=1, dims=1)
#     gt_st[:,0,:] = gt_ends[:,-1,:]
#     pd_st     = torch.roll(pred_ends, shifts=1, dims=1)
#     pd_st[:,0,:] = pred_ends[:,-1,:]

#     gt_samp = sample_bezier_batch(gt_st, gt)    # [B, T, steps, 2]
#     pd_samp = sample_bezier_batch(pd_st, pred)

#     # 7) Expand valid_mask to [B, T, steps, 2]
#     vm = valid_mask[:, :, None, None] \
#          .expand(-1, -1, pd_samp.size(2), pd_samp.size(3)) \
#          .bool()

#     # 8) Shape loss only
#     loss_shape = lambda_shape * F.mse_loss(
#         pd_samp[vm],
#         gt_samp[vm],
#         reduction='mean'
#     )

#     # 9) Backprop & step
#     optimizer.zero_grad()
#     loss_shape.backward()
#     optimizer.step()

#     if debug_mode:
#         print(f"[Batch {batch_idx}] shape={loss_shape:.3f}")

#     return loss_shape.item()

def train_batch(
    model,
    batch,
    optimizer,
    device,
    batch_idx, # For printing progress
    lambda_curve: float = 2000.0,
    lambda_stop:  float = 10.0,
    lambda_len:   float = 20.0, # Weight for the expected length loss
    lambda_type:  float = 100.0,
    debug_mode:   bool  = True,
    # cfg: PolygonConfig = None, # If you need cfg.max_output_segments explicitly
):
    # Move batch to device and ensure lengths is float for calculations
    child_embs  = batch['child_embs'].to(device)
    parent_embs = batch['parent_embs'].to(device)
    parent_bbox = batch['parent_bbox'].to(device)
    parent_segs = batch['parent_bezier_segs'].to(device)
    gt_curves   = batch['gt_curves'].to(device)         # Shape: [B, T_gt, 6]
    lengths     = batch['lengths'].to(device).float()   # Shape: [B], ground truth number of segments

    B = gt_curves.size(0)
    if B == 0: # Handle empty batch if it can occur
        return 0.0
        
    teacher_forcing = False # Standard for training autoregressive models
    geom_scale = 50.0 if teacher_forcing else 10.0 # Scale for geometry loss
    print(1111, child_embs.shape, parent_embs.shape, parent_bbox.shape, parent_segs.shape, gt_curves.shape, lengths.shape)
    # 1) Forward pass
    outputs = model(
        child_embs,
        parent_embs,
        parent_segs,
        # padding_mask=batch.get('parent_padding_mask', None), # Pass if your model uses it
    )
    pred_segments = outputs['segments']     # Shape: [B, S, 6] (S = max_output_segments)
    pred_stops    = outputs['stop_scores']        # Shape: [B, S] (sigmoid probabilities)
    type_logits   = outputs['type_logits']  # Shape: [B, S, 3]

    S = pred_segments.size(1) # Maximum predicted sequence length (max_output_segments)
    T_gt_dim = gt_curves.size(1)  # Padded length of ground truth curves in the batch

    # 2) Create valid_mask based on true lengths for segments that are actually present
    # valid_mask[b, s] = 1 if segment s of sample b is a real segment (0-indexed), 0 otherwise.
    # Max index for a real segment is lengths[b]-1.
    valid_mask = (torch.arange(S, device=device)[None, :] < lengths[:, None]).float()  # Shape: [B, S]

    # 3) Curve L1 loss
    # Compare up to the minimum of predicted length (S) and ground truth padded length (T_gt_dim),
    # then use valid_mask for actual elements.
    compare_len_curve = min(S, T_gt_dim)
    
    pred_for_curve = pred_segments[:, :compare_len_curve, :]    # [B, compare_len_curve, 6]
    gt_for_curve   = gt_curves[:, :compare_len_curve, :]        # [B, compare_len_curve, 6]
    mask_for_curve = valid_mask[:, :compare_len_curve]          # [B, compare_len_curve]

    err = (pred_for_curve - gt_for_curve).abs() * mask_for_curve.unsqueeze(-1)
    num_valid_coords = (mask_for_curve.sum() * 6).clamp(min=1e-9) # Avoid division by zero
    loss_curve = lambda_curve * (err.sum() / num_valid_coords) * geom_scale

    # 4) Stop-token loss (Binary Cross-Entropy)
    # target_stop[b,s] = 1 if s >= (lengths[b]-1) (actual stop index), else 0.
    # Meaning, stop signal should be 0 for segments before the last true segment,
    # and 1 at the last true segment and for all subsequent potential segments.
    idxs_S = torch.arange(S, device=device)[None, :].expand(B, -1) # [B,S] tensor of [0,1,...,S-1]
    stop_idx_gt = (lengths - 1).clamp(min=0)[:, None] # [B,1], index of the last true segment.
    target_stop = (idxs_S >= stop_idx_gt).float()     # [B,S]

    weight_for_stop_loss = torch.where(target_stop > 0, 5.0, 1.0) # As in original
    loss_stop = lambda_stop * F.binary_cross_entropy(
        pred_stops, target_stop, weight=weight_for_stop_loss, reduction='mean'
    ) * geom_scale

    # 5) Expected Length Loss (New count loss)
    # E[L] = sum_{k=0}^{S-1} P(Length > k)
    # P(Length > k) = P(Length >= k+1) = product_{i=0}^{k-1} (1 - pred_stops[:, i])
    # where P(Length > 0) = 1.
    prob_continue = 1.0 - pred_stops # Shape: [B, S]

    ones_for_batch_dim = torch.ones(B, 1, device=device)
    if S == 0:
        expected_length = torch.zeros(B, device=device)
    elif S == 1: # If max output is 1 segment
        expected_length = ones_for_batch_dim.squeeze(1) # Expected length is 1
    else:
        # prob_continue_for_survival should be [1, (1-p0), (1-p1), ..., (1-p_{S-2})]
        # These are the probabilities of *not* stopping before generating segment k+1.
        prob_continue_for_survival = torch.cat([ones_for_batch_dim, prob_continue[:, :-1]], dim=1) # Shape: [B, S]
        survival_probabilities = torch.cumprod(prob_continue_for_survival, dim=1) # Shape: [B, S]
        # survival_probabilities[:, k] is P(Length > k segments) or P(Length >= k+1 segments)
        expected_length = torch.sum(survival_probabilities, dim=1) # Shape: [B]
    
    loss_count = lambda_len * (expected_length - lengths).abs().mean() * geom_scale

    # 6) Type classification loss
    # Uses valid_mask (Shape: [B,S]) to select only relevant logits and labels.
    # type_logits is [B, S, 3].
    print(f"Type logits shape: {type_logits.shape}, valid_mask shape: {valid_mask.shape}")
    type_logits_for_loss = type_logits[valid_mask.bool()] # Shape: [N_valid_total_segments, 3]

    # Derive gt_types from gt_curves. gt_curves is [B, T_gt_dim, 6].
    # We need ground truth types for all positions indicated by valid_mask.
    # Max index in valid_mask is S-1. gt_curves might be shorter than S.
    
    # Determine the source length for gt_types based on T_gt_dim and S
    gt_types_source_len = min(T_gt_dim, S)
    
    gt_c1 = gt_curves[:, :gt_types_source_len, :2]
    gt_c2 = gt_curves[:, :gt_types_source_len, 2:4]

    gt_line_mask_full  = (gt_c1 < 0).all(dim=-1) & (gt_c2 < 0).all(dim=-1)
    gt_quad_mask_full  = (gt_c1 < 0).all(dim=-1) & ~(gt_c2 < 0).all(dim=-1)
    # Original cubic definition: gt_cubic = ~(gt_line | gt_quad)
    gt_cubic_mask_full = ~(gt_line_mask_full | gt_quad_mask_full) 
    
    gt_types_potential = torch.zeros((B, gt_types_source_len), dtype=torch.long, device=device)
    gt_types_potential[gt_quad_mask_full] = 1
    gt_types_potential[gt_cubic_mask_full] = 2
    
    # Ensure gt_types_for_masking has dimension S to match valid_mask
    if gt_types_source_len < S:
        padding_shape = (B, S - gt_types_source_len)
        # Pad with type 0 (line/default). These will be ignored if not in valid_mask.
        padding_types = torch.zeros(padding_shape, dtype=torch.long, device=device) 
        gt_types_for_masking = torch.cat([gt_types_potential, padding_types], dim=1)
    else:
        gt_types_for_masking = gt_types_potential[:, :S] # Ensure it's not longer than S

    gt_types_for_loss = gt_types_for_masking[valid_mask.bool()] # Shape: [N_valid_total_segments]

    if type_logits_for_loss.numel() > 0:
        if type_logits_for_loss.size(0) == gt_types_for_loss.size(0):
            loss_type = lambda_type * F.cross_entropy(type_logits_for_loss, gt_types_for_loss) * geom_scale
        else:
            print(f"Warning: Mismatch in elements for type loss. Logits: {type_logits_for_loss.size(0)}, Labels: {gt_types_for_loss.size(0)}. Setting type loss to 0.")
            loss_type = torch.tensor(0.0, device=device, dtype=total_loss.dtype if 'total_loss' in locals() else torch.float32)
    else: # No valid segments to compute type loss (e.g., all lengths are 0)
        loss_type = torch.tensor(0.0, device=device, dtype=total_loss.dtype if 'total_loss' in locals() else torch.float32)

    # 7) Total loss & backward
    total_loss = loss_curve + loss_stop + loss_count + loss_type
    
    optimizer.zero_grad()
    # If using mixed precision (torch.cuda.amp.autocast), scaler is needed:
    # scaler.scale(total_loss).backward()
    # scaler.step(optimizer)
    # scaler.update()
    total_loss.backward()
    # Optional: Gradient clipping
    # torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
    optimizer.step()

    if debug_mode and (batch_idx % 10 == 0 or batch_idx < 5): # Print more often initially or for specific intervals
        print(
            f"[Batch {batch_idx}] "
            f"Total={total_loss.item():.3f} | "
            f"curve={loss_curve.item():.3f} stop={loss_stop.item():.3f} "
            f"count={loss_count.item():.3f} type={loss_type.item():.3f} | "
            f"E[len]={expected_length.mean().item() if expected_length.numel() > 0 else 0:.2f} "
            f"TrueLen={lengths.mean().item():.2f}"
        )

    return total_loss.item()


def save_checkpoint(model, optimizer, epoch, current_best_loss, checkpoint_path):
    # Handle potential torch.compile wrapper for model.state_dict()
    unwrapped_model = model
    if hasattr(model, "_orig_mod"):
        unwrapped_model = model._orig_mod
   
    full_state_dict = unwrapped_model.state_dict()
   
    # Filter out frozen T5 encoder parameters (this is what's taking 418MB!)
    filtered_state_dict = {
        k: v for k, v in full_state_dict.items()
        if not k.startswith("encoder.")  # Remove T5 encoder weights
           and not k.startswith("_orig_mod.encoder.")  # For compiled models
    }
    
    print(f"Original model size: {len(full_state_dict)} parameters")
    print(f"Filtered model size: {len(filtered_state_dict)} parameters")
    
    # Calculate size reduction
    original_params = sum(p.numel() for p in full_state_dict.values())
    filtered_params = sum(p.numel() for p in filtered_state_dict.values())
    original_size_mb = original_params * 4 / (1024 * 1024)
    filtered_size_mb = filtered_params * 4 / (1024 * 1024)
    
    print(f"Size reduction: {original_size_mb:.1f} MB → {filtered_size_mb:.1f} MB")
    
    checkpoint = {
        "epoch": epoch,
        "model_state_dict": filtered_state_dict,
        'optimizer_state_dict': optimizer.state_dict(),
        'best_loss': current_best_loss,
    }
   
    torch.save(checkpoint, checkpoint_path)
    
    if os.path.exists(checkpoint_path):
        file_size_mb = os.path.getsize(checkpoint_path) / (1024 * 1024)
        print(f"Checkpoint saved to {checkpoint_path} ({file_size_mb:.2f} MB)")
    else:
        print(f"Error: Failed to save checkpoint to {checkpoint_path}")
    return checkpoint


def train_model_batched(
    dataset_path,
    model_name=None,# Checkpoint path to resume from
    output_dir="bezier_checkpoints_overfit",
    num_epochs=100,
    learning_rate=9e-3,
    batch_size=None,
    max_samples=None,
    run_visualization=False,
):
    import traceback # For detailed error in visualization

    os.makedirs(output_dir, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[Training run] Device: {device}")

    ds = AugmentedDataset(root_dir=dataset_path, max_samples=max_samples)
    if len(ds) == 0: raise RuntimeError("Empty dataset!")
    N = len(ds)
    actual_batch_size = N if batch_size is None else batch_size
    loader = DataLoader(
        ds,
        batch_size=actual_batch_size,
        shuffle=True,
        num_workers=0,      # <–– no subprocesses
        pin_memory=False,   # if your CPU→GPU transfer is a bottleneck, try toggling
        collate_fn=collate_fn
    )

    # It's good practice to have model configuration in one place
    # For now, using the hardcoded values as per your PolygonPredictor snippet
    cfg=PolygonConfig()
    

    model = PolygonPredictor(
        cfg=cfg
    ).to(device)

    optimizer = torch.optim.Adam(
        filter(lambda p: p.requires_grad, model.parameters()), lr=learning_rate,betas=(0.9, 0.95),weight_decay=0.0
    )
    # scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=30, gamma=0.1) # Example: Add if needed

    start_epoch = 1
    best_loss = float('inf') # Initialize to infinity for proper comparison

    if model_name and os.path.exists(model_name):
        print(f"Resuming training from checkpoint: {model_name}")
        ckpt = torch.load(model_name, map_location=device)
        
        # Determine if the model to load into was compiled
        model_to_load = model
        if hasattr(model, "_orig_mod"): # If 'model' is already compiled
            model_to_load = model._orig_mod

        # model_state_dict in ckpt is already filtered (custom weights only)
        # load_state_dict with strict=False will load matching keys and ignore others (like CLIP)
        missing_keys, unexpected_keys = model_to_load.load_state_dict(ckpt["model_state_dict"], strict=False)
        if unexpected_keys:
            print(f"Warning: Unexpected keys found in checkpoint's model_state_dict: {unexpected_keys}")
        
        # Verify that essential custom parts were loaded (optional detailed check)
        # custom_keys_not_loaded = [k for k in missing_keys if not (k.startswith("clip_model.") or k.startswith("_orig_mod.clip_model."))]
        # if custom_keys_not_loaded:
        #     print(f"Warning: Some custom model parameters were not found in the checkpoint or not loaded: {custom_keys_not_loaded}")

        if "optimizer_state_dict" in ckpt:
            optimizer.load_state_dict(ckpt["optimizer_state_dict"])
            for state in optimizer.state.values():
                for k_opt, v_opt in state.items():
                    if isinstance(v_opt, torch.Tensor):
                        state[k_opt] = v_opt.to(device)
        else:
            print("Warning: Optimizer state not found in checkpoint.")

        if "epoch" in ckpt:
            start_epoch = ckpt['epoch'] + 1
            print(f"Resuming from epoch {start_epoch}")
        else:
            print("Warning: Epoch number not found in checkpoint. Starting from epoch 1.")
            
        if "best_loss" in ckpt:
            best_loss = ckpt['best_loss']
            print(f"Resuming with best_loss: {best_loss:.6f}")
        else:
            print(f"Warning: best_loss not found in checkpoint. Initializing to infinity.")
        
        # if scheduler and "scheduler_state_dict" in ckpt and ckpt["scheduler_state_dict"]:
        #     scheduler.load_state_dict(ckpt["scheduler_state_dict"])
        #     print("Resumed LR scheduler state.")

        print(f"LOADED checkpoint successfully.")
    else:
        if model_name:
            print(f"Warning: Checkpoint file '{model_name}' not found. Training from scratch.")
        else:
            print("No checkpoint specified. Training from scratch.")
    # best_loss*=50
    best_model_path = os.path.join(output_dir, "best_model.pth") # Unified best model name

    torch.backends.cudnn.benchmark = True
    if torch.__version__ >= "2.0.0": # torch.compile is stable in 2.0+
        print("Attempting to compile model with torch.compile()...")
        try:
            # Important: If you compile, ensure saving/loading handles the _orig_mod attribute
            # or load state_dict into the uncompiled model before compiling.
            # The current loading logic tries to load into model_to_load (which would be uncompiled if 'model' isn't compiled yet).
            # If 'model' is already compiled when loading, model_to_load = model._orig_mod is correct.
            model = torch.compile(model)
            print("Model compiled successfully.")
        except Exception as e: # Catch broader exceptions as compile can fail for various reasons
            print(f"Model compilation failed: {e}. Proceeding without compilation.")
    
    # scaler = GradScaler() # Initialize if using mixed precision

    print(f"Starting training from epoch {start_epoch} up to {num_epochs}.")
    print(f"Batch size: {actual_batch_size}. Overfitting {N} samples if batch_size == N.")

    for epoch in range(start_epoch, num_epochs + 1):
        with torch.cuda.amp.autocast():
            model.train()
        epoch_total_loss = 0.0
        # Store individual losses for epoch average
        epoch_losses_components = {"stop": 0.0, "se": 0.0, "curve": 0.0, "len": 0.0, "start": 0.0}
        

        for batch_idx, batch_data in enumerate(loader):
            # Assuming train_batch now returns a tuple of all loss components
            # total_loss_item, stop_item, se_item, curve_item, len_item, start_item
            loss_value = train_batch(
                model, batch_data, optimizer, device, batch_idx, # Pass current best_loss
            )
            
            epoch_total_loss += loss_value
            

        avg_epoch_loss = epoch_total_loss / len(loader) if len(loader) > 0 else float('nan')
        log_msg = f"Epoch {epoch:3d}/{num_epochs} — Total Loss: {avg_epoch_loss:.6f}"
        for name, total_val in epoch_losses_components.items():
            avg_comp_loss = total_val / len(loader) if len(loader) > 0 else float('nan')
            log_msg += f" — {name}L: {avg_comp_loss:.4f}"
        print(log_msg)

        # if scheduler: scheduler.step()

        if avg_epoch_loss < best_loss:
            best_loss = avg_epoch_loss
            # best_epoch_val = epoch # Track the epoch for the best model
            print(f"  → New best model at epoch {epoch}, loss {best_loss:.6f}")
            save_checkpoint(model, optimizer, epoch, best_loss, best_model_path)

        if avg_epoch_loss < 1e-6: # Early stopping for overfit
            print(f"✨ Perfect overfit at epoch {epoch}!")
            break
        
        if torch.cuda.is_available():
            import gc
            gc.collect()
            torch.cuda.empty_cache()

    final_path = os.path.join(output_dir, "final_model.pth")
    # Save final model using the epoch number of the last completed epoch and the overall best_loss found
    save_checkpoint(model, optimizer, num_epochs, best_loss, final_path) 
    print(f"Saved final model custom weights to {final_path}")

    # Load the best model for visualization
    if os.path.exists(best_model_path):
        print(f"Loading best model from {best_model_path} for visualization.")
        best_ckpt = torch.load(best_model_path, map_location=device)
        vis_epoch_num = best_ckpt.get('epoch', 'unknown') # Get epoch from best checkpoint

        # Re-initialize model for visualization to avoid issues with compiled model state for visualization
        # Use the same configuration used for training
        vis_model = PolygonPredictor(
            cfg=cfg
        ).to(device)
        
        model_to_load_vis = vis_model
        if hasattr(vis_model, "_orig_mod"): # Should not be compiled yet as it's fresh
             model_to_load_vis = vis_model._orig_mod

        model_to_load_vis.load_state_dict(best_ckpt["model_state_dict"], strict=False)
        print(f"Best model (epoch {vis_epoch_num}) loaded for visualization.")

        if run_visualization:
            try:
                visualize_predictions(
                    model=vis_model, dataset=ds, device=device,
                    output_dir=output_dir, epoch=f"best_e{vis_epoch_num}"
                )
                print(f"Visualizations saved under {output_dir}/vis_epoch_best_e{vis_epoch_num}")
            except Exception as e:
                print(f"Visualization failed: {e}")
                traceback.print_exc()
    else:
        print(f"Best model checkpoint not found at {best_model_path}. Skipping visualization.")

    return model


# --- Visualization ---

import os, random
import numpy as np
import torch
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon # Ensure rename if needed

# --- Visualization Helper Functions (Keep as before) ---
def bezier_curve(p0, c1, c2, p1, n_points=20):
    """Sample points along a single cubic Bezier segment."""
    t = np.linspace(0, 1, n_points)[:, np.newaxis]
    points = ( (1 - t)**3 * p0 +
               3 * (1 - t)**2 * t * c1 +
               3 * (1 - t) * t**2 * c2 +
               t**3 * p1 )
    return points # [n_points, 2]

def render_bezier_sequence(curve_params_tensor: torch.Tensor, initial_p0: np.ndarray, n_samples_per_segment=15):
    """
    Renders a sequence of Bezier curves defined by [C1, C2, P_end].
    """
    if curve_params_tensor is None or curve_params_tensor.numel() == 0:
        return np.empty((0, 2), dtype=np.float32)

    curve_params_np = curve_params_tensor.detach().cpu().numpy()
    K = curve_params_np.shape[0]
    all_rendered_points = []
    # Ensure initial_p0 is float32 numpy array
    current_p0 = np.array(initial_p0, dtype=np.float32).reshape(2)


    for i in range(K):
        c1 = curve_params_np[i, 0:2]
        c2 = curve_params_np[i, 2:4]
        p1 = curve_params_np[i, 4:6] # Endpoint for this segment
        segment_points = bezier_curve(current_p0, c1, c2, p1, n_samples_per_segment)
        # Add points excluding the first one for subsequent segments to avoid duplication
        all_rendered_points.append(segment_points if i == 0 else segment_points[1:])
        current_p0 = p1 # Update start point for the next segment

    if not all_rendered_points:
        return np.empty((0, 2), dtype=np.float32)

    return np.concatenate(all_rendered_points, axis=0)
import matplotlib.patches as patches
def visualize_predictions(model, dataset, device, output_dir, epoch, max_vis=20):
    """
    Visualizes predicted vs. ground-truth Bézier sequences,
    batching each sample to size=1 so it matches the model.
    """
    def render_segment(start_pt, seg, seg_type, steps=50):
        # …your existing implementation…
        if seg_type == 0:
            end_pt = seg[-2:]
            return np.vstack([start_pt, end_pt])
        elif seg_type == 1:
            ctrl_pt, end_pt = seg[-4:-2], seg[-2:]
            t = np.linspace(0,1,steps)[:,None]
            B0 = (1-t)**2; B1 = 2*(1-t)*t; B2 = t**2
            return B0*start_pt + B1*ctrl_pt + B2*end_pt
        else:
            p1, p2, p3 = seg[:2], seg[2:4], seg[4:6]
            t = np.linspace(0,1,steps)[:,None]
            B0 = (1-t)**3; B1 = 3*(1-t)**2*t
            B2 = 3*(1-t)*t**2; B3 = t**3
            return B0*start_pt + B1*p1 + B2*p2 + B3*p3

    vis_dir = os.path.join(output_dir, f"vis_epoch_{epoch}")
    os.makedirs(vis_dir, exist_ok=True)
    model.to(device).eval()

    indices = random.sample(range(len(dataset)), min(len(dataset), max_vis))
    for i in indices:
        sample = dataset[i]
        try:
            # — batchify each field —
            ce = sample['child_embs'].unsqueeze(0).to(device)           # [1, seq_len, d]
            pe = sample['parent_embs'].unsqueeze(0).to(device)          # [1, seq_len, d]
            bb = sample['parent_bbox'].unsqueeze(0).to(device)          # [1,4,2]
            pseg = sample['parent_bezier_segs'].unsqueeze(0).to(device)# [1,T,6]
            gt   = sample['gt_curves']                                  # [T_gt,6]

            # draw bbox rectangle in normalized coords
            bb0 = bb[0].cpu().numpy()
            xmin, ymin = bb0[:,0].min(), bb0[:,1].min()
            width, height = bb0[:,0].ptp(), bb0[:,1].ptp()
            rect = patches.Rectangle(
                (xmin, ymin),
                width, height,
                linewidth=1, edgecolor='gray',
                facecolor='none', linestyle='--'
            )

            # model forward
            with torch.no_grad():
                out = model(
                    ce, pe, pseg
                )

            pred_segs  = out['segments'][0].cpu().numpy()    # [T,6]
            pred_types = out['type_logits'][0].cpu().numpy().argmax(-1)
            gt_segs    = gt.cpu().numpy()                    # [T_gt,6]
            print("pred_segs",pred_segs)
            # compute start/end points
            gt_ends   = gt_segs[:,4:6]; pred_ends = pred_segs[:,4:6]
            gt_starts = np.roll(gt_ends, 1, axis=0);  gt_starts[0]  = gt_ends[-1]
            pr_starts = np.roll(pred_ends, 1, axis=0); pr_starts[0] = pred_ends[-1]

            # plot
            fig, ax = plt.subplots(figsize=(6,6))
            ax.set_aspect('equal','box')
            ax.set_xlim(-0.05,1.05); ax.set_ylim(-0.05,1.05)
            ax.set_title(f"Sample {i}")
            ax.add_patch(rect)

            # GT
            for seg, st in zip(gt_segs, gt_starts):
                negs = int((seg[:4]==-1).sum())
                stype = 0 if negs==4 else 1 if negs==2 else 2
                curve = render_segment(st, seg, stype, steps=100)
                ax.plot(curve[:,0], curve[:,1], '-', color='green', lw=2)

            # Pred
            for seg, st, stype in zip(pred_segs, pr_starts, pred_types):
                curve = render_segment(st, seg, stype, steps=100)
                ax.plot(curve[:,0], curve[:,1], '--', color='red', lw=2)

            plt.tight_layout()
            plt.savefig(os.path.join(vis_dir, f"sample_{i}.png"), dpi=120)
            plt.close(fig)

        except Exception as e:
            print(f"Error visualizing sample {i}: {e}")
            traceback.print_exc()

    model.train()

# =============================================================================
# Main Entry Point
# =============================================================================
if __name__ == "__main__":
    # --- Create Dummy Data (if needed for testing) ---
    # (Consider adding a small dummy dataset creation here if running standalone)
    create_dummy = False
    if create_dummy:
        print("Setting up dummy dataset structure for testing...")
        dummy_root = "dummy_bezier_dataset"
        os.makedirs(os.path.join(dummy_root, "json"), exist_ok=True)
        os.makedirs(os.path.join(dummy_root, "masks/scene1"), exist_ok=True)
        os.makedirs(os.path.join(dummy_root, "images"), exist_ok=True)
        dummy_json = {
            "scene": [{"id": 0, "description": "background", "mask_path": None, "parent": -1},
                      {"id": 1, "description": "a wiggly shape", "mask_path": "wiggly_mask.png", "parent": 0}]}
        with open(os.path.join(dummy_root, "json/scene1.json"), 'w') as f: json.dump(dummy_json, f)
        img_w, img_h = 100, 100
        dummy_image = Image.new('RGB', (img_w, img_h), color='white')
        dummy_image.save(os.path.join(dummy_root, "images/scene1.png"))
        # Create a more complex mask for Bezier testing
        mask = np.zeros((img_h, img_w), dtype=np.uint8)
        pts = np.array([[10, 50], [30, 20], [50, 50], [70, 80], [90, 50]], np.int32)
        cv2.polylines(mask, [pts], isClosed=False, color=255, thickness=10) # Make a thick wiggly line
        # Ensure it's filled for findContours
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if cnts: cv2.drawContours(mask, cnts, -1, 255, -1) # Fill based on contour
        Image.fromarray(mask).save(os.path.join(dummy_root, "masks/scene1/wiggly_mask.png"))
        print("Dummy data created.")
        dataset_to_train = dummy_root
    else:
        dataset_to_train = "dataset" # Use your actual dataset path here

    # --- Train ---
    print(f"Using dataset: {dataset_to_train}")
    train_model_batched(
        dataset_path=dataset_to_train,
        model_name="", # Load previous best if exists
        # model_name="bezier_checkpoints/best_model.pth", # Load previous best if exists
        output_dir="bezier_checkpoints",
        num_epochs=30,
        learning_rate=5e-3, # Slightly lower LR might be stabler
        batch_size=5,
        run_visualization=True,
    )
    