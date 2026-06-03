# Third-Party Notices

This file records third-party notices for the current Agentic Mesh repository
dependency set. Regenerate and review this file before each public release.

This notice file is informational and does not replace the license terms of the
listed projects.

## Runtime Dependencies

### PyYAML

- Project: https://pyyaml.org/
- Source: https://github.com/yaml/pyyaml
- License: MIT
- Used for: loading YAML configuration files.

### OpenTelemetry API

- Project: https://opentelemetry.io/
- Source: https://github.com/open-telemetry/opentelemetry-python
- Package: `opentelemetry-api`
- License: Apache License 2.0
- Used for: telemetry API integration.

### OpenTelemetry SDK

- Project: https://opentelemetry.io/
- Source: https://github.com/open-telemetry/opentelemetry-python
- Package: `opentelemetry-sdk`
- License: Apache License 2.0
- Used for: telemetry SDK integration.

### OpenTelemetry OTLP gRPC Exporter

- Project: https://opentelemetry.io/
- Source: https://github.com/open-telemetry/opentelemetry-python
- Package: `opentelemetry-exporter-otlp-proto-grpc`
- License: Apache License 2.0
- Used for: exporting telemetry through OTLP over gRPC.

## Development Dependencies

### pytest

- Project: https://pytest.org/
- Source: https://github.com/pytest-dev/pytest
- License: MIT
- Used for: test execution.

### setuptools

- Project: https://setuptools.pypa.io/
- Source: https://github.com/pypa/setuptools
- License: MIT
- Used for: Python package build backend.

## Container Images

### Python

- Image: `python:3.12-slim`
- Project: https://www.python.org/
- Image source: https://github.com/docker-library/python
- License: Python Software Foundation License and third-party notices from the
  image distribution.
- Used for: Agentic Mesh runtime container base image.

### OpenTelemetry Collector Contrib

- Image: `otel/opentelemetry-collector-contrib:0.101.0`
- Project: https://opentelemetry.io/
- Source: https://github.com/open-telemetry/opentelemetry-collector-contrib
- License: Apache License 2.0
- Used for: local telemetry collection in Docker Compose.

## Notes For Maintainers

- Verify transitive dependencies before each release.
- Include any generated package lock, SBOM, or license scan output with release
  artifacts when practical.
- Avoid adding GPL, LGPL, AGPL, SSPL, Commons Clause, Polyform, BSL, Elastic
  License, or custom source-available dependencies to the open source core
  without explicit review.
