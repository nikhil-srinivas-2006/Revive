# RRDBNet inference architecture

Source: XPixelGroup/BasicSR, revision
`8d56e3a045f9fb3e1d8872f92ee4a4f07f886b0a`, Apache-2.0; see LICENSE.
The upstream copyright and license text are retained.

`rrdbnet_arch.py` removes BasicSR registry integration and imports three local
helpers instead of the full BasicSR package. `compat.py` includes those three
upstream helpers and their required imports only. Initialization retains the
upstream no-gradient decorator. No trained forward computation is changed.
Exact upstream and packaged hashes are recorded in upstream.json.

The Real-ESRGAN author's x2plus checkpoint is acquired separately under the
Real-ESRGAN BSD-3-Clause project license (LICENSE-Real-ESRGAN). Weights are not
included in the wheel. Their release URL, size and SHA-256 are in the model
catalog. Acquisition from HTTPS is not a claim of a publisher-signed digest.
