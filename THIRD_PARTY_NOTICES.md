# Third-party notices

MeshHome depends on third-party packages that are installed at build time and are not vendored in
this repository. The main ones are listed below. Full license texts ship with each package.

| Package | License |
| --- | --- |
| meshcore (meshcore_py) | MIT |
| FastAPI, Starlette, Pydantic, Uvicorn | MIT / BSD-3-Clause |
| SQLAlchemy, Alembic | MIT |
| asyncpg | Apache-2.0 |
| argon2-cffi | MIT |
| React, React DOM, React Router | MIT |
| TanStack Query | MIT |
| Tailwind CSS | MIT |
| lucide-react | ISC |
| Leaflet | BSD-2-Clause |
| uqr (QR code generation), jsQR (QR code scanning) | MIT / Apache-2.0 |
| unicode-emoji-json (emoji data, compacted at build time) | MIT |

Map data shown by default is © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors,
available under the Open Database License. Tiles are fetched at runtime and are not distributed with
this project.

No third-party source code is copied into this repository (see `docs/upstream-reuse.md`).

## Downloaded at install time (native Raspberry Pi installs, optional)

When you turn on **HTTPS**, the installer downloads a prebuilt [lego](https://github.com/go-acme/lego)
binary (MIT licence) from its GitHub releases to get and renew the certificate. It is not distributed
with MeshHome; the version and SHA-256 checksums are pinned in `deploy/native/lego.lock`.

When you set up the **radio HAT**, the installer downloads a prebuilt
[ZephCore](https://github.com/liquidraver/ZephCore) binary (MIT licence; it includes Monocypher,
BSD-2-Clause OR CC0-1.0, and is built on Zephyr RTOS, Apache-2.0) from its GitHub releases. It is
not distributed with MeshHome; the exact version and SHA-256 checksums are pinned in
`deploy/native/zephcore.lock`.
