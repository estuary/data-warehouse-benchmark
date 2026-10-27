# How This Codebase Works (`docs/`)

These docs describe the benchmark harness as it is, written from the source.

| Doc | Read it when you want to… |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Understand how the harness works: CLI surface, module map, SQL execution model, cost/correctness models, data pipeline, results schema. **Start here.** |
| [`E2E_LOADING.md`](E2E_LOADING.md) | Run the full generate->load->benchmark pipeline (`benchmark e2e`). |
| [`OPERATOR_GUIDE.md`](OPERATOR_GUIDE.md) | Operate a benchmark run as the human overseeing it. |

The rest live next to the code they describe:

| Doc | Read it when you want to… |
|---|---|
| [`../src/benchmark/README.md`](../src/benchmark/README.md) | Find your way around the Python harness's modules. |
| [`../queries/README.md`](../queries/README.md) | Understand the query suite's layout and how each engine's SQL is produced. |
| [`../data/README.md`](../data/README.md) | Know what the generated data and row-count manifests under `data/` are. |
| [`../estuary/README.md`](../estuary/README.md) | Work with the Estuary capture and materialization specs. |
| [`../terraform/README.md`](../terraform/README.md) | Provision or tear down the engines and shared infrastructure. |
| [`../scripts/README.md`](../scripts/README.md) | Use the resize, sweep and maintenance utilities that sit outside the CLI. |
| [`../report/README.md`](../report/README.md) | Know what under `report/` is authored prose and what is rebuilt from the database. |
| [`../viz/README.md`](../viz/README.md) | Build or modify the client-side explorer. |

The results database ships with the repo via Git LFS, so everything these
describe can be rebuilt from a clone: see "Getting the repo" in the root
[`README.md`](../README.md).
