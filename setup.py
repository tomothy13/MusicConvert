from setuptools import setup

# Build a console/script-based bundle that runs in Terminal (no Tk GUI)
OPTIONS = {
    "argv_emulation": True,
}

setup(
    scripts=["main.py"],
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)