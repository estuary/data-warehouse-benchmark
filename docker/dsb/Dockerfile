# Builds the microsoft/dsb data generator (dsdgen) on Linux — sidesteps the
# macOS C-port. DSB shares the TPC-DS toolkit; the value skew lives in its
# distribution files (tpcds.idx + *.dst), so plain dsdgen run against DSB's
# checkout emits skewed DSB data.
#
# Build:  docker build -t benchmark-dsb docker/dsb
# Run:    docker run --rm -v "$PWD/data/dsb/sf1:/out" benchmark-dsb -SCALE 1 -DIR /out -FORCE
#         (entrypoint runs ./dsdgen from the tools dir so it finds tpcds.idx)
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential make gcc bison flex \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/microsoft/dsb.git /opt/dsb

WORKDIR /opt/dsb/code/tools
# TPC-DS toolkit builds from Makefile.suite with the target OS set (both as a
# make var and pinned in the file, since some revisions only honour one).
RUN sed -i 's/^OS[[:space:]]*=.*/OS = LINUX/' Makefile.suite \
 && make -f Makefile.suite OS=LINUX dsdgen \
 && ./dsdgen -h >/dev/null 2>&1 || true

# Run from the tools dir so dsdgen resolves tpcds.idx + the .dst skew tables.
ENTRYPOINT ["./dsdgen"]
CMD ["-h"]
