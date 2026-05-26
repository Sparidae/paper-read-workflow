# Batch Import Papers to Notion

You can use the `batch` command with the `--continue-on-error` flag. This flag ensures the tool keeps processing all papers even when individual ones fail -- it will log the error and move on to the next paper instead of stopping.

## Exact Command

```bash
uv run paper-tool batch <your-file.md> --continue-on-error
```

Replace `<your-file.md>` with the path to your markdown file. The file should contain one URL per line (lines starting with `#` are treated as comments and ignored).

## What Happens on Failure

When a single paper fails (e.g. a broken URL, a PDF that cannot be extracted, or an LLM call that times out), the tool will:

1. Print a clear error message to stderr indicating which URL failed and why.
2. Skip that paper and immediately continue to the next one.
3. All successfully imported papers will still be written to Notion.

At the end of the run, you will see a summary showing how many papers succeeded and how many failed, along with the specific failed URLs.

## How to Handle Failures Afterward

For each failed paper, re-run individually with the `--debug` flag to diagnose the root cause:

```bash
uv run paper-tool add --debug <failed-url>
```

The `--debug` flag prints raw prompts and LLM responses, which is the quickest way to understand whether the problem is with the source PDF, the extraction pipeline, or the LLM classification step. Once you have fixed the issue (e.g. corrected a URL or adjusted a config), you can import it with the normal `add` command.
