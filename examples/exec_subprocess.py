import subprocess

def run(cmd):
    subprocess.Popen(cmd, shell=True)
