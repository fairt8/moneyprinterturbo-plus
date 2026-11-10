"""Video models that fit one free 16 GB T4 (Kaggle "GPU T4 x2" = two separate T4s, Colab free = one T4).

Turing (T4, compute 7.5) has no native bf16 and no FlashAttention-2, so everything here runs in fp16 with PyTorch SDPA,
model CPU offload and VAE tiling. Kaggle gives ~29 GB system RAM, which the offload needs.

  Wan21T2V   Wan-AI/Wan2.1-T2V-1.3B-Diffusers (Apache-2.0, commercial OK). Text-to-video, 480p. ~8 GB VRAM with offload.
             Default for normal scene clips. Several minutes per ~3 s clip on a T4 (estimate, untested here).
  LTXT2V     Lightricks/LTX-Video-0.9.5 (2B, LTX open-weights licence). Text-to-video, much faster than Wan, lower quality.
  Wan22TI2VDiffusers  Wan-AI/Wan2.2-TI2V-5B-Diffusers (Apache-2.0). Experimental on a T4: needs offload, 480p-ish sizes only.
  Hunyuan15  (PROPOSED, untested on a T4) tencent HunyuanVideo-1.5 8.3B via diffusers HunyuanVideo15*Pipeline.
             "hy15-i2v": 480p image-to-video STEP-DISTILLED (8-12 steps) - animates a still (e.g. the FLUX scene image).
             "hy15-t2v": 480p text-to-video CFG-distilled (needs ~30-50 steps, so ~3-4x slower than i2v).
             The fp16 transformer (16.6 GB) + Qwen2.5-VL-7B text encoder (~14 GB) do not fit a 16 GB T4 or Kaggle's 29 GB RAM
             as-is, so both are loaded 4-bit NF4 with bitsandbytes (~5 GB each) + model CPU offload + VAE tiling.
             One worker per kernel is the safe setting (KAGGLE_PARALLEL=1) until a selftest proves two fit in RAM.
             Licence: Tencent Hunyuan Community License (not valid in the EU, UK, South Korea; >100M MAU needs a licence).

Image-to-video for Animated Story stays in worker.py (LTXI2V = LTX-Video 2B). One model instance per GPU:
run one worker process per GPU with CUDA_VISIBLE_DEVICES=0 / 1 (kaggle/kernel_main.py does this).
pip install -U diffusers transformers accelerate sentencepiece imageio imageio-ffmpeg ftfy
"""
import os


def _dtype(torch):
    """fp16 on Turing/Volta (T4, V100), bf16 only on Ampere+ (torch.cuda.is_bf16_supported() can report emulated bf16 on T4)."""
    return torch.bfloat16 if torch.cuda.get_device_capability()[0] >= 8 else torch.float16


def _frames(seconds, fps, cap, mult):
    """Frame count of the form mult*k+1, capped (longer narration is covered by looping / holding in assembly)."""
    n = min(cap, max(mult + 1, int(seconds * fps)))
    return (n - 1) // mult * mult + 1


class Wan21T2V:
    """Wan2.1 T2V-1.3B through diffusers WanPipeline (same weights as the official repo, no flash-attn needed)."""
    def __init__(self, model=None, steps=None, max_frames=None):
        import torch
        from diffusers import AutoencoderKLWan, WanPipeline
        self.torch = torch
        mid = model or os.environ.get("WAN_T2V_MODEL", "Wan-AI/Wan2.1-T2V-1.3B-Diffusers")
        vae = AutoencoderKLWan.from_pretrained(mid, subfolder="vae", torch_dtype=torch.float32)   # fp32 VAE avoids fp16 NaNs
        self.pipe = WanPipeline.from_pretrained(mid, vae=vae, torch_dtype=_dtype(torch))
        self.pipe.enable_model_cpu_offload()
        try: self.pipe.vae.enable_tiling()
        except Exception: pass
        self.steps = int(steps or os.environ.get("WAN_STEPS", 30))
        self.max_frames = int(max_frames or os.environ.get("WAN_MAX_FRAMES", 49))       # 49 @16fps ~ 3 s; 81 = 5 s (slower)

    def __call__(self, c, out):
        from diffusers.utils import export_to_video
        w, h = (832, 480) if c["width"] >= c["height"] else (480, 832)                      # 1.3B is trained for 480p
        n = _frames(c.get("seconds") or 3, 16, self.max_frames, 4)
        frames = self.pipe(prompt=c["prompt"], negative_prompt=c.get("negative") or
                           "blurry, low quality, distorted, watermark, text, static, jittery",
                           height=h, width=w, num_frames=n, guidance_scale=5.0, num_inference_steps=self.steps,
                           generator=self.torch.Generator("cpu").manual_seed(42 + int(c.get("idx", 0)))).frames[0]
        export_to_video(frames, out, fps=16); self.torch.cuda.empty_cache()


class LTXT2V:
    """LTX-Video 2B text-to-video (diffusers LTXPipeline). Fast fallback on a T4; sizes divisible by 32, 8k+1 frames."""
    def __init__(self, model=None, steps=None):
        import torch
        from diffusers import LTXPipeline
        self.torch = torch
        self.pipe = LTXPipeline.from_pretrained(model or os.environ.get("LTX_MODEL", "Lightricks/LTX-Video-0.9.5"), torch_dtype=_dtype(torch))
        self.pipe.enable_model_cpu_offload(); self.pipe.vae.enable_tiling()
        self.steps = int(steps or os.environ.get("LTX_STEPS", 30))

    def __call__(self, c, out):
        from diffusers.utils import export_to_video
        w, h = (c["width"] // 32) * 32, (c["height"] // 32) * 32
        n = _frames(c.get("seconds") or 4, 24, 97, 8)
        frames = self.pipe(prompt=c["prompt"], negative_prompt="worst quality, inconsistent motion, blurry, jittery, distorted",
                           width=w, height=h, num_frames=n, num_inference_steps=self.steps, guidance_scale=3.0,
                           decode_timestep=0.05, decode_noise_scale=0.025,
                           generator=self.torch.Generator("cpu").manual_seed(42 + int(c.get("idx", 0)))).frames[0]
        export_to_video(frames, out, fps=24); self.torch.cuda.empty_cache()


class Wan22TI2VDiffusers:
    """Wan2.2 TI2V-5B via diffusers (experimental on a T4: offload + small sizes). Text-to-video or image-to-video."""
    def __init__(self, model=None):
        import torch
        from diffusers import AutoencoderKLWan, WanPipeline
        self.torch = torch
        mid = model or "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
        vae = AutoencoderKLWan.from_pretrained(mid, subfolder="vae", torch_dtype=torch.float32)
        self.pipe = WanPipeline.from_pretrained(mid, vae=vae, torch_dtype=_dtype(torch))
        self.pipe.enable_model_cpu_offload()

    def __call__(self, c, out):
        from diffusers.utils import export_to_video
        w, h = (832, 480) if c["width"] >= c["height"] else (480, 832)
        n = _frames(c.get("seconds") or 3, 24, 73, 4)
        frames = self.pipe(prompt=c["prompt"], height=h, width=w, num_frames=n, guidance_scale=5.0, num_inference_steps=30,
                           generator=self.torch.Generator("cpu").manual_seed(42 + int(c.get("idx", 0)))).frames[0]
        export_to_video(frames, out, fps=24); self.torch.cuda.empty_cache()


class Hunyuan15:
    """HunyuanVideo-1.5 480p on one T4: NF4 transformer + NF4 text encoder, CPU offload, tiled VAE. Seconds are capped short
    (default 61 frames @24 fps ~2.5 s) because T4 attention cost grows ~quadratically with frames; assembly loops/holds."""
    REPOS = {"i2v": "hunyuanvideo-community/HunyuanVideo-1.5-Diffusers-480p_i2v_step_distilled",
             "t2v": "hunyuanvideo-community/HunyuanVideo-1.5-Diffusers-480p_t2v_distilled"}

    def __init__(self, mode="i2v"):
        import torch
        from diffusers import PipelineQuantizationConfig
        self.torch, self.mode = torch, mode
        mid = os.environ.get("HY15_%s_MODEL" % mode.upper(), self.REPOS[mode])
        dt = torch.bfloat16 if os.environ.get("HY15_DTYPE") == "bf16" else _dtype(torch)   # fp16 on T4; bf16 if fp16 gives NaN/black
        q = PipelineQuantizationConfig(quant_backend="bitsandbytes_4bit",
                                       quant_kwargs={"load_in_4bit": True, "bnb_4bit_quant_type": "nf4", "bnb_4bit_compute_dtype": dt},
                                       components_to_quantize=os.environ.get("HY15_QUANTIZE", "transformer,text_encoder").split(","))
        if mode == "i2v":
            from diffusers import HunyuanVideo15ImageToVideoPipeline as P
        else:
            from diffusers import HunyuanVideo15Pipeline as P
        self.pipe = P.from_pretrained(mid, quantization_config=q, torch_dtype=dt)
        self.pipe.enable_model_cpu_offload()
        try: self.pipe.vae.enable_tiling()
        except Exception: pass
        self.steps = int(os.environ.get("HY15_STEPS", 8 if mode == "i2v" else 30))   # i2v step-distilled: 8 fast / 12 best
        self.max_frames = int(os.environ.get("HY15_MAX_FRAMES", 61))                    # 4k+1; 121 = 5 s (much slower)

    def __call__(self, c, out):
        import base64, io
        import numpy as np
        from diffusers.utils import export_to_video
        n = _frames(c.get("seconds") or 3, 24, self.max_frames, 4)
        g = self.torch.Generator("cpu").manual_seed(42 + int(c.get("idx", 0)))
        neg = c.get("negative") or "blurry, low quality, distorted, watermark, text, subtitles, static, jittery"
        if self.mode == "i2v":
            from PIL import Image
            if not c.get("image_b64"): raise RuntimeError("hy15-i2v needs scene.image_b64")
            img = Image.open(io.BytesIO(base64.b64decode(c["image_b64"]))).convert("RGB")   # size/aspect come from the image
            frames = self.pipe(image=img, prompt=c["prompt"], negative_prompt=neg, num_frames=n,
                               num_inference_steps=self.steps, generator=g).frames[0]
        else:
            w, h = (848, 480) if c["width"] >= c["height"] else (480, 848)
            frames = self.pipe(prompt=c["prompt"], negative_prompt=neg, height=h, width=w, num_frames=n,
                               num_inference_steps=self.steps, generator=g).frames[0]
        arr = np.asarray(frames[len(frames) // 2], dtype=np.float32)
        if not np.isfinite(arr).all() or arr.max() < 0.02:                              # fp16 overflow -> black/NaN frames
            raise RuntimeError("HunyuanVideo produced black/NaN frames (try HY15_DTYPE=bf16)")
        export_to_video(frames, out, fps=24); self.torch.cuda.empty_cache()


T2V = {"wan21-diffusers": Wan21T2V, "ltx-t2v": LTXT2V, "wan22-5b-diffusers": Wan22TI2VDiffusers,
       "hy15-i2v": lambda: Hunyuan15("i2v"), "hy15-t2v": lambda: Hunyuan15("t2v")}
MODEL_NAMES = list(T2V)
