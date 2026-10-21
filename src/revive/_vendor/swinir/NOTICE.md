# SwinIR inference architecture

Source: https://github.com/JingyunLiang/SwinIR
Revision: 6545850fbf8df298df73d81f3e8cba638787c8bd
License: Apache-2.0 (LICENSE). The original authorship header is preserved.
SwinIR acknowledges Swin-Transformer and KAIR code. Their MIT notices are retained
in LICENSE-Swin-Transformer and LICENSE-KAIR, with original Git blob IDs recorded.

Only the timm import is redirected to local compatibility helpers. Evaluation
DropPath is an identity; stochastic-depth training is rejected. Initialization
uses native PyTorch trunc_normal_. to_2tuple supplies integer/tuple conversion.
No torchvision, timm or model download is imported.

upstream.json records original and packaged byte hashes. compat.py is Revive code;
the architecture layers and forward mathematics remain unchanged. Weights are
explicitly installed from the author's v0.0 release, never bundled.
