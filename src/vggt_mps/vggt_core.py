"""
Core VGGT processing module
"""

import torch
import numpy as np
from PIL import Image
from pathlib import Path
from typing import List, Dict, Optional, Union
import sys

# Add VGGT repo to path
REPO_PATH = Path(__file__).parent.parent / "repo" / "vggt"
if REPO_PATH.exists():
    sys.path.insert(0, str(REPO_PATH))


class VGGTProcessor:
    """VGGT model processor for 3D reconstruction"""

    def __init__(self, device: Union[str, torch.device] = "mps"):
        """
        Initialize VGGT processor

        Args:
            device: Device to run model on (mps, cuda, cpu)
        """
        self.device = torch.device(device) if isinstance(device, str) else device
        self.model = None
        self.dtype = torch.float32 if self.device.type == "mps" else torch.float16

    def load_model(self, model_path: Optional[Path] = None):
        """
        Load VGGT model

        Args:
            model_path: Optional path to model weights
        """
        if self.model is not None:
            return  # Already loaded

        try:
            from vggt.models.vggt import VGGT
        except ImportError:
            print("⚠️ VGGT module not found. Using simulated mode.")
            return

        if model_path is None:
            # Default paths to check
            possible_paths = [
                Path(__file__).parent.parent / "models" / "vggt_model.pt",
                Path(__file__).parent.parent / "repo" / "vggt" / "vggt_model.pt",
            ]
            for path in possible_paths:
                if path.exists():
                    model_path = path
                    break

        if model_path and model_path.exists():
            print(f"📂 Loading model from: {model_path}")
            self.model = VGGT()
            checkpoint = torch.load(model_path, map_location=self.device)
            self.model.load_state_dict(checkpoint)
            self.model = self.model.to(self.device)
        else:
            print("📥 Loading model from HuggingFace...")
            try:
                self.model = VGGT.from_pretrained("facebook/VGGT-1B").to(self.device)
            except Exception as e:
                print(f"⚠️ Could not load model: {e}")
                self.model = None

        if self.model:
            self.model.eval()
            print("✅ Model loaded successfully!")

    def process_images(self, images: List[np.ndarray]) -> Union[List[np.ndarray], Dict]:
        """
        Process images through VGGT

        Args:
            images: List of images as numpy arrays (H, W, 3)

        Returns:
            Depth maps and optionally other predictions
        """
        # Ensure model is loaded
        if self.model is None:
            self.load_model()

        if self.model is None:
            # Fallback to simulated depth
            print("⚠️ Using simulated depth (model not available)")
            return self._simulate_depth(images)

        # Process with real model
        try:
            from vggt.utils.load_fn import load_and_preprocess_images

            # Save images temporarily for VGGT loader
            import tempfile
            temp_dir = Path(tempfile.mkdtemp())
            temp_paths = []

            for i, img in enumerate(images):
                temp_path = temp_dir / f"input_{i:03d}.jpg"
                if isinstance(img, np.ndarray):
                    Image.fromarray(img).save(temp_path)
                else:
                    img.save(temp_path)
                temp_paths.append(str(temp_path))

            # Load and preprocess
            input_tensor = load_and_preprocess_images(temp_paths).to(self.device)

            # Run inference
            with torch.no_grad():
                if self.device.type == "mps":
                    predictions = self.model(input_tensor)
                else:
                    with torch.cuda.amp.autocast(dtype=self.dtype):
                        predictions = self.model(input_tensor)

            # Extract depth maps
            depth_tensor = predictions['depth'].cpu().numpy()
            depth_maps = [depth_tensor[0, i, :, :, 0] for i in range(depth_tensor.shape[1])]

            # Clean up temp files
            for path in temp_paths:
                Path(path).unlink()
            temp_dir.rmdir()

            # Return full predictions dict if available
            result = {
                'depth_maps': depth_maps,
                'camera_poses': predictions.get('poses', None),
                'point_cloud': self._generate_point_cloud(images, depth_maps)
            }

            return result

        except Exception as e:
            print(f"⚠️ Error processing with real model: {e}")
            return self._simulate_depth(images)

    def _simulate_depth(self, images: List[np.ndarray]) -> List[np.ndarray]:
        """Generate simulated depth maps for testing"""
        depth_maps = []
        for img in images:
            if isinstance(img, np.ndarray):
                h, w = img.shape[:2]
            else:
                w, h = img.size

            # Create radial depth pattern
            center_x, center_y = w // 2, h // 2
            y_coords, x_coords = np.ogrid[:h, :w]
            distances = np.sqrt((x_coords - center_x)**2 + (y_coords - center_y)**2)

            # Normalize to depth range
            max_dist = np.sqrt(center_x**2 + center_y**2)
            depth = 5.0 + 3.0 * (1.0 - distances / max_dist)

            # Add some noise
            depth += np.random.randn(h, w) * 0.2

            depth_maps.append(depth)

        return depth_maps

    def _generate_point_cloud(
        self,
        images: List[np.ndarray],
        depth_maps: List[np.ndarray],
        step: int = 10
    ) -> np.ndarray:
        """
        Generate 3D point cloud from depth maps

        Args:
            images: Input images
            depth_maps: Depth maps
            step: Downsampling step for visualization

        Returns:
            Nx3 array of 3D points
        """
        all_points = []

        for i, (img, depth) in enumerate(zip(images, depth_maps)):
            h, w = depth.shape

            # Camera parameters (simplified)
            fx = fy = 500
            cx, cy = w/2, h/2

            # Create pixel grid
            xx, yy = np.meshgrid(np.arange(0, w, step), np.arange(0, h, step))

            # Sample depth
            z = depth[::step, ::step]

            # Back-project to 3D
            x = (xx - cx) * z / fx + i * 2  # Offset each view
            y = (yy - cy) * z / fy

            # Stack points
            points = np.stack([x.flatten(), y.flatten(), z.flatten()], axis=-1)
            all_points.append(points)

        return np.vstack(all_points)