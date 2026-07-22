# Contributing to MinbarLive

Thank you for your interest in contributing to MinbarLive! Contributions are welcome and appreciated.

## Feedback

Before contributing, you might want to share feedback or report issues:

- **GitHub Issues**: [Open an issue](https://github.com/MinbarLive/MinbarLive/issues) for bugs or feature requests
- **Google Forms**: [Submit feedback](https://forms.gle/DJ3F25HKrrLjH9h59) anonymously

## How to Contribute

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Run the tests (`python -m pytest`)
4. Commit your changes (`git commit -m 'Add amazing feature'`)
5. Push to the branch (`git push origin feature/amazing-feature`)
6. Open a Pull Request

## Development Setup

```bash
# Clone the repo
git clone https://github.com/MinbarLive/MinbarLive.git
cd MinbarLive

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/macOS

# Install dependencies
pip install -r requirements.txt

# Run tests
python -m pytest
```

## Running Tests

```bash
# Run all tests
python -m pytest

# Run with verbose output
python -m pytest -v

# Run a specific test file
python -m pytest tests/test_dictionary.py

# Run with coverage (requires pytest-cov)
python -m pytest --cov=.
```

## Code Style

This project uses [Ruff](https://github.com/astral-sh/ruff) for linting and formatting. Configuration is in `ruff.toml`.

## Areas Where Contributions Are Especially Welcome

- **Additional language sources for Quran translations** - Curated sources currently exist for German, English, Turkish, Albanian, and Bosnian; more are welcome
- **Hadith translation database** - We haven't found a suitable open database yet
- **UI/UX improvements**
- **Documentation improvements**
- **Bug fixes and performance optimizations**

## Adding New Quran Translation Languages

The app now supports multiple target languages via language-specific JSON files:

```
data/translations/
├── quran/
│   ├── de.json    # German (Bubenheim & Elyas)
│   ├── en.json    # English (add your source)
│   └── ...        # Other languages
├── athan/
│   ├── de.json    # German
│   └── ...        # Other languages
```

To add a new language:

1. Find a reliable translation source (e.g., [quranenc.com](https://quranenc.com) has many languages)
2. Copy `notebooks/build_quran_dict.py` and modify:
   - Change `language` to the correct ISO 639-1 code
   - Change `translation_key` to your language's endpoint
3. Run the script to generate the new dictionary
4. Create a matching `data/translations/athan/{language}.json` file
5. The app will automatically use these when the target language matches

Example for French:

```python
language = "fr"
translation_key = "french_hamidullah"
```

## Regenerating Quran Embeddings (`data/embeddings/quran_embeddings.json`)

This file is now tracked with **Git LFS** and will be downloaded automatically when you clone the repository (if you have Git LFS installed). If you need to regenerate it:

- Run the Jupyter notebook: `notebooks/Build_Quran_EmbeddingSpace.ipynb`
- Embeddings take ~10-15 minutes to generate and require an OpenAI API key.

## License

By contributing, you agree that your contributions will be licensed under the AGPL-3.0 License.
