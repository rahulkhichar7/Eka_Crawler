# Eca-Curator Web Crawler

**Modular, scalable, and production-ready web crawler and data curation pipeline.**  
Part of the Eca-Curator mega data curation project.

**Eca-Curator Web Crawler** is a robust, multi-process web crawling and data curation pipeline. Designed for scalability and maintainability, it supports domain-aware crawling, WARC archiving, robots.txt compliance, and clean separation of concerns. This repository is a core component of the larger Eca-Curator data curation ecosystem.

---

## Features

- **Domain-aware, depth-limited crawling**
- **Multi-process architecture** for high performance
- **WARC file archiving** for reproducible data curation
- **Robots.txt compliance** using Protego
- **Graceful shutdown and resumability**
- **Configurable via `config.py`** (including `JOIN_TIMEOUT` for process joining)
- **Logging and progress tracking**
- **Modular codebase** for easy extension

---

## Getting Started

### 1. Clone the Repository

```bash
git clone https://github.com/rahulkhichar7/eca-curator-web-crawler.git
cd eca-curator-web-crawler
```

### 2. Install Dependencies

We recommend using a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Prepare the Seed File

Create a `seed.txt` file in the project root.  
Each line should contain a domain or starting URL to crawl, e.g.:

```
https://example.com
https://anotherdomain.org
```

### 4. Configure Settings

Open `config.py` to adjust parameters:

- `MAX_DEPTH`: Maximum crawl depth per domain
- `USER_AGENT`: User agent string for requests
- `TIMEOUT`: Request timeout (seconds)
- `JOIN_TIMEOUT`: Timeout for process joining (in seconds)
- `WARC_SIZE_LIMIT`: Max WARC file size before rotating
- ...and more

### 5. Run the Crawler

```bash
python main.py
```

Crawled data, WARC files, and logs will be saved in their respective folders.

---

## Configuration

All major settings are in `config.py`.  
**Example:**

```python
# config.py
MAX_DEPTH = 10
USER_AGENT = 'EcaCuratorBot/1.0'
TIMEOUT = 10
JOIN_TIMEOUT = 2  # Used for process.join(timeout=JOIN_TIMEOUT)
...
```

---

## Directory Structure

```
eca-curator-web-crawler/
│
├── config.py
├── utils.py
├── writers.py
├── crawler.py
├── main.py
├── requirements.txt
├── seed.txt
├── warc_files/
├── files/
└── README.md
```

- `warc_files/`: Stores WARC archives of crawled web data
- `files/`: Stores logs and lists of visited/timeout URLs

---

## Logging

Logs are written to `crawler.log` by default.  
You can adjust log level and format in `utils.py` or `config.py`.

---

## Extending & Integrating

- The codebase is modular and OOP-friendly.
- You can add new writers, processing steps, or integrate with other Eca-Curator modules.
- For large-scale or distributed crawling, adapt the process management logic as needed.

---

## Troubleshooting

- **No data saved?** Check your `seed.txt` and ensure target domains are reachable.
- **Timeouts?** Increase `TIMEOUT` in `config.py`.
- **Process hangs on shutdown?** Adjust `JOIN_TIMEOUT` as needed.

---

## Contributing

Pull requests and suggestions are welcome!  
Please file issues or feature requests via [GitHub Issues](https://github.com/rahulkhichar7/eca-curator-web-crawler/issues).

---

## License

[MIT License](LICENSE)

---

## Acknowledgements

- [Protego](https://github.com/scrapy/protego) for robots.txt parsing
- [warcio](https://github.com/webrecorder/warcio) for WARC file support

---

**Part of the [Eca-Curator] data curation project.**

---

*Happy crawling!* 🕸️

**Note** - This is only phase 1 code, full pipeline code is not updated yet.
