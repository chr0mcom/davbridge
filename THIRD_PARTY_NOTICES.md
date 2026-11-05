# Third-party notices

DAVbridge itself is licensed under the [MIT License](LICENSE); see also [NOTICE](NOTICE).
This file lists third-party material that is distributed with DAVbridge, either in this
repository or in the container image `ghcr.io/chr0mcom/davbridge`.

Exact versions and file hashes of the Python packages are pinned in `requirements/runtime.txt`.
The authoritative license and notice texts are included in each package's
`*.dist-info/` directory inside the image (and in the corresponding source distribution);
this list is an inventory, not a replacement for those texts. The container image's SBOM
(SPDX JSON, one file per platform, attached to every GitHub release and embedded in the image as an attestation) lists the complete set of packages.

## 1. Data files

### Unicode CLDR `windowsZones.xml`

`data/windowsZones.xml` is an unmodified copy of `common/supplemental/windowsZones.xml` from the
[Unicode CLDR](https://github.com/unicode-org/cldr) project. It maps Windows time zone identifiers to
IANA time zone names. The file header carries the notice "Copyright © 1991-2013 Unicode, Inc."
and refers to <http://www.unicode.org/copyright.html>. Provenance and SHA-256 checksum are recorded
in `data/README.md`.

The data is provided under the **Unicode License v3**
(SPDX: `Unicode-3.0`, <https://www.unicode.org/license.txt>). The license text as published
there is reproduced below, as the license requires.

<details>
<summary>Unicode License v3 (full text)</summary>

```text
UNICODE LICENSE V3

COPYRIGHT AND PERMISSION NOTICE

Copyright © 1991-2026 Unicode, Inc.

NOTICE TO USER: Carefully read the following legal agreement. BY
DOWNLOADING, INSTALLING, COPYING OR OTHERWISE USING DATA FILES, AND/OR
SOFTWARE, YOU UNEQUIVOCALLY ACCEPT, AND AGREE TO BE BOUND BY, ALL OF THE
TERMS AND CONDITIONS OF THIS AGREEMENT. IF YOU DO NOT AGREE, DO NOT
DOWNLOAD, INSTALL, COPY, DISTRIBUTE OR USE THE DATA FILES OR SOFTWARE.

Permission is hereby granted, free of charge, to any person obtaining a
copy of data files and any associated documentation (the "Data Files") or
software and any associated documentation (the "Software") to deal in the
Data Files or Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, and/or sell
copies of the Data Files or Software, and to permit persons to whom the
Data Files or Software are furnished to do so, provided that either (a)
this copyright and permission notice appear with all copies of the Data
Files or Software, or (b) this copyright and permission notice appear in
associated Documentation.

THE DATA FILES AND SOFTWARE ARE PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF
THIRD PARTY RIGHTS.

IN NO EVENT SHALL THE COPYRIGHT HOLDER OR HOLDERS INCLUDED IN THIS NOTICE
BE LIABLE FOR ANY CLAIM, OR ANY SPECIAL INDIRECT OR CONSEQUENTIAL DAMAGES,
OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,
ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THE DATA
FILES OR SOFTWARE.

Except as contained in this notice, the name of a copyright holder shall
not be used in advertising or otherwise to promote the sale, use or other
dealings in these Data Files or Software without prior written
authorization of the copyright holder.
```

</details>

## 2. Python runtime dependencies

These packages are installed into the container image from `requirements/runtime.txt`
(direct dependencies are marked with an asterisk).

| Package | License (SPDX) | Project |
|---|---|---|
| fastapi * | MIT | <https://github.com/fastapi/fastapi> |
| starlette * | BSD-3-Clause | <https://github.com/encode/starlette> |
| uvicorn * | BSD-3-Clause | <https://github.com/encode/uvicorn> |
| httpx * | BSD-3-Clause | <https://github.com/encode/httpx> |
| httpcore | BSD-3-Clause | <https://github.com/encode/httpcore> |
| h11 | MIT | <https://github.com/python-hyper/h11> |
| anyio | MIT | <https://github.com/agronholm/anyio> |
| idna | BSD-3-Clause | <https://github.com/kjd/idna> |
| certifi | MPL-2.0 | <https://github.com/certifi/python-certifi> |
| click | BSD-3-Clause | <https://github.com/pallets/click> |
| lxml * | BSD-3-Clause | <https://github.com/lxml/lxml> |
| icalendar * | BSD-2-Clause | <https://github.com/collective/icalendar> |
| python-dateutil | BSD-3-Clause and Apache-2.0 (dual) | <https://github.com/dateutil/dateutil> |
| tzdata * | Apache-2.0 | <https://github.com/python/tzdata> |
| pydantic | MIT | <https://github.com/pydantic/pydantic> |
| pydantic-core | MIT | <https://github.com/pydantic/pydantic> |
| annotated-types | MIT | <https://github.com/annotated-types/annotated-types> |
| annotated-doc | MIT | <https://github.com/fastapi/annotated-doc> |
| opentelemetry-api | Apache-2.0 | <https://github.com/open-telemetry/opentelemetry-python> (API only, no SDK or exporter is installed; nothing is sent) |
| typing-extensions | PSF-2.0 | <https://github.com/python/typing_extensions> |
| typing-inspection | MIT | <https://github.com/pydantic/typing-inspection> |

Notes:

- **certifi** is licensed under MPL-2.0. It is used unmodified; its source is available at the link above.
- **lxml** binary wheels statically include native libraries, notably libxml2 and libxslt
  (MIT license) and zlib (zlib license). Their notices are part of the lxml distribution
  metadata; see <https://lxml.de/> for details.
- **tzdata** packages the [IANA time zone database](https://www.iana.org/time-zones), which is in the public domain.
- The list reflects the dependency set at the time of writing. If `requirements/runtime.txt` and this
  table differ, `requirements/runtime.txt` and the release SBOM are authoritative; please open an
  issue or pull request to correct this file.

## 3. Runtime and base image

The container image is based on the official Python 3.12 slim image (Debian). It contains:

- **CPython**, licensed under the [PSF License Version 2](https://docs.python.org/3/license.html);
- **Debian packages** of the base image, each under its own license; the license texts are in
  `/usr/share/doc/<package>/copyright` inside the image.

The base image is pinned by digest in the `Dockerfile`.

## 4. Development and CI tooling (not distributed in the image)

Test and lint tools (for example pytest, Ruff) and the GitHub Actions used in
`.github/workflows/` are used to build and verify the project but are not part of the
distributed runtime image. They are subject to their own licenses.
