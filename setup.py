import sys
from setuptools import setup, Extension


def main():
    args = dict(
        name="libminigrpc",
        version="0.0.1",
        description="Minimal gRPC client",
        packages=["libminigrpc"],
        zip_safe=True,
        install_requires=["httpcore[http2]==0.12.0"],
    )

    setup(**args)


if __name__ == "__main__":
    main()
