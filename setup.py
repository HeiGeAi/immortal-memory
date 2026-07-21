"""Compatibility entry point for older PEP 517 frontends."""

from pathlib import Path
from setuptools import setup


ROOT = Path(__file__).resolve().parent


setup(
    name="immortal-memory",
    version=(ROOT / "core" / "VERSION").read_text(encoding="utf-8").strip(),
    description="Local-first personal memory layer for AI agents",
    python_requires=">=3.9",
    packages=["immortal_memory"],
    package_dir={"immortal_memory": "core"},
    include_package_data=True,
    package_data={
        "immortal_memory": [
            "VERSION",
            "*.json",
            "*.md",
            "agents/*.yaml",
            "product_assets/*.css",
            "product_assets/*.js",
            "product_assets/views/*.js",
        ]
    },
    entry_points={
        "console_scripts": ["immortal-memory=immortal_memory.cli:main"]
    },
)
