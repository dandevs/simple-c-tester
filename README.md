# simple-c-tester

C test runner with a Textual TUI. Scans `c/tests/` for `*.c` files, compiles each with gcc via a generated Makefile, runs the executable, and reports results.

Suites and tests are displayed as a tree with Unicode box-drawing characters (`├──`, `└──`, `│`). Test output is shown inline in bordered boxes (`╭─╮`, `│`, `╰─╯`) beneath each test that has output. Click an output box to view the full output.

## Requirements

- Python >= 3.9
- gcc (for compiling C tests)
- make

## Quick Start

```bash
pip install -r requirements.txt
python3 src/main.py
```

The test path is hardcoded as `c/tests`. Place your `*.c` test files there.

## CLI Flags

| Flag | Default | Description |
|---|---|---|
| `--parallel N` | 4 | Number of concurrent test runners |
| `--watch` | off | Watch for file changes, re-run affected tests |
| `--output-lines N` | 25 | Max output lines per inline output box |
| `--theme ansi\|default` | `ansi` | UI theme. `ansi` blends with terminal colors, `default` uses Textual's dark theme |

## Building a Portable PEX

The project can be packaged into a single `.pex` file using [PEX](https://docs.pex-tool.org/). The resulting file bundles the application and all Python dependencies (rich, textual, watchdog) with native extensions for Linux, macOS, and Windows.

### Prerequisites

```bash
pip install pex
```

### Build

```bash
./build_pex.sh
```

Output: `out/simple-c-tester.pex`

### Included Platforms

The PEX bundles watchdog native extensions for:

| Platform | Flag |
|---|---|
| Linux x86_64 | `manylinux2014_x86_64` |
| Linux ARM64 | `manylinux2014_aarch64` |
| macOS Intel | `macosx_10_9_x86_64` |
| macOS Apple Silicon | `macosx_11_0_arm64` |
| Windows x86_64 | `win_amd64` |

All targeting Python 3.9. To change the target Python version, edit the `--platform` flags in `build_pex.sh` (e.g. `cp-312-cp312` for Python 3.12).

### Running the PEX

```bash
./out/simple-c-tester.pex
```

Python 3.9 must be installed on the target machine. gcc and make are still required for compiling C tests. The PEX must be run from a directory containing the `c/tests/` folder.

## Development

### Installation

```bash
pip install -r requirements.txt
```

### Run

```bash
python3 src/main.py
```

Run from the repo root. Do **not** use `python3 -m src.main` — imports use bare module names (`from models import ...`) which rely on the `sys.path` setup in `main.py`.

### Project Structure

```
src/
  main.py          entry point, CLI argument parsing
  app.py           TestRunnerApp (Textual TUI)
  state.py         shared mutable state
  models/          Test, Suite, AppState dataclasses, TestState enum
  render/          UI rendering (tree, labels, output boxes, styles)
  runner/          test execution, Makefile generation, build state
  watch/           file system watching (watchdog)
c/
  tests/           C test source files
test_build/        compiled executables and generated Makefile (gitignored)
```

### Packaging Config

`pyproject.toml` configures the package for PEX builds:

- `setuptools.packages.find` with `where = ["src"]` discovers packages (`models`, `render`, `runner`, `watch`)
- `py-modules` lists top-level modules (`main`, `app`, `state`)
- `project.scripts` defines the `simple-c-tester` console script entry point
