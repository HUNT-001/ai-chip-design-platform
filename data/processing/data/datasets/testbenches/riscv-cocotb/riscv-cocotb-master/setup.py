import setuptools

with open("README.rst", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="riscv-cocotb",
    use_scm_version={
        "relative_to": __file__,
        "write_to": "riscvcocotb/version.py",
    },
    author="Stefan Wallentowitz",
    author_email="stefan@wallentowitz.de",
    description="RISC-V cocotb testbench",
    long_description=long_description,
    url="https://github.com/wallento/riscv-cocotb",
    packages=setuptools.find_packages(),
    install_requires=[
        'riscv-model',
    ],
    setup_requires=[
        'setuptools_scm',
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
