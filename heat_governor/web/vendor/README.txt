Vendored third-party assets for the heat-governor mesh dashboard (heat-governor mesh serve).
They live here so the page renders offline and makes no request to any other host.
Each asset's licence file sits beside it. Fetched 2026-11-08.

file: react.production.min.js
source: https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js
version: React 18.3.1
licence: react-LICENSE.txt (MIT)
sha256: d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd

file: react-dom.production.min.js
source: https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js
version: ReactDOM 18.3.1
licence: react-LICENSE.txt (MIT)
sha256: 35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d

file: react-LICENSE.txt
source: https://unpkg.com/react@18.3.1/LICENSE
sha256: 52412d7bc7ce4157ea628bbaacb8829e0a9cb3c58f57f99176126bc8cf2bfc85

file: htm.js
source: https://unpkg.com/htm@3.1.1/dist/htm.js
version: htm 3.1.1
licence: htm-LICENSE.txt (Apache-2.0)
sha256: 80e39afe20fd61183412eda89efa10532d57945e6364642aceacd50eb2384b4b

file: htm-LICENSE.txt
source: https://unpkg.com/htm@3.1.1/LICENSE
sha256: 740725f7252e750af735d0028cc534970772f513331e9f68150fede8fb3ce00f

file: lato-latin-300.woff2
source: https://fonts.gstatic.com/s/lato/v25/S6u9w4BMUTPHh7USSwiPGQ.woff2
version: Lato v25, weight 300, latin subset (Google Fonts)
licence: lato-OFL.txt (SIL Open Font License 1.1)
sha256: 115f6a626ca115d4ad5581b59275327e0e860b30330a52b0f785561332dd2429

file: lato-latin-400.woff2
source: https://fonts.gstatic.com/s/lato/v25/S6uyw4BMUTPHjx4wXg.woff2
version: Lato v25, weight 400, latin subset (Google Fonts)
licence: lato-OFL.txt (SIL Open Font License 1.1)
sha256: 918b7dc3e2e2d015c16ce08b57bcb64d2253bafc1707658f361e72865498e537

file: lato-latin-700.woff2
source: https://fonts.gstatic.com/s/lato/v25/S6u9w4BMUTPHh6UVSwiPGQ.woff2
version: Lato v25, weight 700, latin subset (Google Fonts)
licence: lato-OFL.txt (SIL Open Font License 1.1)
sha256: c447dd7677b419db7b21dbdfc6277c7816a913ffda76fd2e52702df538de0e49

file: lato-latin-900.woff2
source: https://fonts.gstatic.com/s/lato/v25/S6u9w4BMUTPHh50XSwiPGQ.woff2
version: Lato v25, weight 900, latin subset (Google Fonts)
licence: lato-OFL.txt (SIL Open Font License 1.1)
sha256: 08664859baab5ed98f0bf818ed77e38464ff1826dc6406d5ecbd651409afbd92

file: lato-OFL.txt
source: https://raw.githubusercontent.com/google/fonts/main/ofl/lato/OFL.txt
sha256: 74ba064d03f1f1c4a952da936c3eb71866c34404916734de3cae73b34357e59e

The woff2 URLs come from https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&display=swap
fetched with a desktop Chrome User-Agent; only the latin subset is vendored. The @font-face rules
are inline in ../index.html and point at /vendor/<file>.woff2.
