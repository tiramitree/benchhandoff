FROM docker.io/library/python@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

COPY --chown=65532:65532 src/benchhandoff /opt/benchhandoff/app/benchhandoff

USER 65532:65532
ENTRYPOINT ["python", "-I", "-B", "-u", "-c", "import runpy,sys;sys.path.insert(0,'/opt/benchhandoff/app');runpy.run_module('benchhandoff.controller_step',run_name='__main__')"]
