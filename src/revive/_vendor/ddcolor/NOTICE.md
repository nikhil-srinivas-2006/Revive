# DDColor inference architecture

Upstream: https://github.com/piddnad/DDColor
Revision: `2adb63f2656ac41cbdf7b894cddd94121a3faf13`

DDColor is by Xiaoyang Kang, Tao Yang, Wenqi Ouyang, Peiran Ren, Lingzhi Li and
Xuansong Xie, DAMO Academy, Alibaba Group. Distributed under Apache-2.0; see LICENSE.
ConvNeXt contains Meta Platforms copyright and is also accompanied by its original
MIT license in LICENSE-ConvNeXt. Position encoding retains its original Facebook /
DETR attribution (Apache-2.0).

Only the model and four architecture helpers are included. The four BasicSR import
prefixes in model.py are changed to relative imports. No training framework,
automatic model downloads, Hugging Face remote-code loader or upstream inference
loader is used. No architecture mathematics is intentionally changed.

`upstream.json` records source and packaged digests. The developer refresh script
is `scripts/vendor_ddcolor.py`. Keep upstream license/copyright headers intact.
