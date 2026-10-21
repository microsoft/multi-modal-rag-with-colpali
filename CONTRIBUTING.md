## Contributing

This project welcomes contributions and suggestions.  Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit https://cla.opensource.microsoft.com.

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Development Setup

### Pre-commit Hooks

This project uses pre-commit hooks to ensure code quality and consistency. The hooks automatically run linting, formatting, and other checks before each commit.

#### Install Pre-commit Hooks

1. Install pre-commit:
   ```bash
   pip install pre-commit
   ```

2. Install the git hooks:
   ```bash
   pre-commit install
   ```

#### Run Hooks Manually

To run all pre-commit hooks on all files:
```bash
pre-commit run --all-files
```

#### Configured Hooks

- **Ruff**: Python linting and formatting
- **Codespell**: Spell checking
- **File quality checks**: End-of-file fixer, trailing whitespace removal
- **JSON/TOML validation**: Format and validate configuration files

All pull requests are automatically checked by GitHub Actions to ensure these standards are met.

### Tests

Install Python 3.14 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Run these commands from the repository root in PowerShell:

```powershell
Push-Location modules\agent
uv run --frozen python -m pytest tests -q
Pop-Location

Push-Location modules\document_processor
uv run --frozen python -m unittest discover -s tests
Pop-Location

Push-Location modules\colpali_inference
$env:PYTHONPATH = "src"
uv run --frozen python -m unittest discover -s tests
Remove-Item Env:PYTHONPATH
Pop-Location
```

These tests need no Azure credentials, GPU, or model downloads. CI runs all three
suites, validates Bicep and Helm, and builds the five container images without
pushing them or deploying resources.
