import tarfile

def extract(name):
    with tarfile.open(name) as tf:
        tf.extractall('.')
