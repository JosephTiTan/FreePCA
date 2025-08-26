# FreePCA：Integrating Consistency Information across Long-short Frames in Training-free Long Video Generation via Principal Component Analysis([arxiv link](https://arxiv.org/abs/2505.01172))

## Overview
![](overview.jpg)

## Setup (based on [Videocrafter2](https://github.com/AILab-CVC/VideoCrafter/tree/main))

### 1. Install Environment via Anaconda (Recommended)
```bash
conda create -n freepca python=3.8.5
conda activate freepca
pip install -r requirements.txt
```

### 2. Download pretrained T2V models via [Hugging Face](https://huggingface.co/VideoCrafter/VideoCrafter2/blob/main/model.ckpt), and put the `model.ckpt` in `checkpoints/base_512_v2/model.ckpt`.
## 
|T2V-Models|Resolution|Checkpoints|
|:---------|:---------|:--------|
|VideoCrafter2|320x512|[Hugging Face](https://huggingface.co/VideoCrafter/VideoCrafter2/blob/main/model.ckpt)
|VideoCrafter1|576x1024|[Hugging Face](https://huggingface.co/VideoCrafter/Text2Video-1024/blob/main/model.ckpt)
|VideoCrafter1|320x512|[Hugging Face](https://huggingface.co/VideoCrafter/Text2Video-512/blob/main/model.ckpt)

### 3. Input the following commands in terminal.
```bash
sh scripts/run_text2video.sh
```

## Citation
@inproceedings{tan2025freepca,
  title={Freepca: Integrating consistency information across long-short frames in training-free long video generation via principal component analysis},
  author={Tan, Jiangtong and Yu, Hu and Huang, Jie and Xiao, Jie and Zhao, Feng},
  booktitle={Proceedings of the Computer Vision and Pattern Recognition Conference},
  pages={27979--27988},
  year={2025}
}

## Acknowledgements
Our codebase builds on [Videocrafter2](https://github.com/AILab-CVC/VideoCrafter/tree/main). Thanks the authors for sharing their awesome codebases! 
 
