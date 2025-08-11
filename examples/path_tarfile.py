import tarfile

def extract(fp):
    with tarfile.open(fp) as tar:
        tar.extractall(".")
