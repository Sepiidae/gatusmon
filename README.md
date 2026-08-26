# FAU OIT Service Monitor

A lightweight, real-time service status dashboard built with Python, Flask, and Gunicorn. This application monitors various service endpoints, categorizes them into groups, and provides a visual status overview (OK, Warning, Critical, Unknown) with detailed diagnostics and contact information.

## 🚀 Features

- **Real-time Monitoring**: Background polling of service endpoints at configurable intervals.
- **Dynamic Grouping**: Services are automatically grouped based on configuration rules.
- **Visual Dashboard**: A compact, multi-column grid layout for high-density status viewing.
- **Detailed Diagnostics**: Click on any service to view host information, latency, and specific condition results.
- **Contact Integration**: Displays assigned support contacts (name, phone, email) for each service.
- **Filtering**: Supports inclusion and exclusion patterns via `config.json`.
- **Dockerized**: Ready for deployment with a provided `Dockerfile`.

## 🛠️ Tech Stack

- **Backend**: Python, Flask
- **Web Server**: Gunicorn (with threads for concurrency)
- **Frontend**: HTML5, CSS3 (Modern UI), Vanilla JavaScript
- **Deployment**: Docker

## 📂 Project Structure

```
.
├── app.py              # Main Flask application and background worker
├── config.json         # Configuration for endpoints, filters, and contacts
├── Dockerfile          # Containerization instructions
├── requirements.txt    # Python dependencies
├── startDevelop        # Script to run in development mode
├── startProd           # Script to run in production mode (Gunicorn)
└── templates/
    └── index.html      # Dashboard frontend
```

## ⚙️ Configuration (`config.json`)

The application behavior is driven by `config.json`. Key sections include:

- `endpoints`: A list of URLs to poll.
- `filters`: 
    - `exclude`: Patterns to ignore specific services.
    - `include`: Rules to map services to specific groups.
- `service_contacts`: Specific contact info for individual services.
- `default_group_contacts`: Contact info for entire groups.
- `refresh_interval_seconds`: How often the background worker fetches data.

## 🏃 How to Run

### Development Mode
To run the application locally with Flask's development server:
```bash
chmod +x startDevelop
./startDevelop
```
The app will be available at `http://localhost:5001`.

### Production Mode
To run the application using Gunicorn:
```bash
chmod +x startProd
./startProd
```

### Docker
To build and run the application using Docker:
```bash
docker build -t fau-monitor .
docker run -p 5001:5001 fau-monitor
```

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
