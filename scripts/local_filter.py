import logging
import json

# Configure logging - adjust level as needed (DEBUG, INFO, WARNING, ERROR, CRITICAL)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Default configuration - override with environment variables or config file later
DEFAULT_CONFIG = {
    "allowed_fields": ["text", "category"],  # Example: Only allow these fields to be sent
    "log_all_data": False, # Set to True for verbose debugging/auditing (use sparingly!)
}

def load_config():
    """Loads configuration from environment variables or a config file.
       Prioritizes environment variables over the default config."""

    config = DEFAULT_CONFIG.copy()  # Start with defaults

    # Example: Could expand to read from JSON/YAML config file here if needed.
    # With open("config.json", "r") as f:
    #     config.update(json.load(f))

    return config


def sanitize_data(data, config):
    """Sanitizes the data by filtering fields based on allowed fields."""

    allowed_fields = config["allowed_fields"]
    sanitized_data = {}

    for field, value in data.items():
        if field in allowed_fields:
            sanitized_data[field] = value
        else:
            logging.warning(f"Field '{field}' removed from data due to filtering policy.")
            if config["log_all_data"]:
                logging.debug(f"Filtered out field: {field} with value: {value}")

    return sanitized_data


def log_data_flow(data, provider, reason, config):
    """Logs information about data flow."""
    log_message = f"Data sent to '{provider}' due to '{reason}'. Fields: {list(data.keys())}"
    logging.info(log_message)



# Example Usage (for testing - replace with actual integration point)
if __name__ == "__main__":
    config = load_config()
    sample_data = {"text": "This is some text.", "category": "example", "secret_field": "sensitive info"}

    logging.info(f"Original Data: {sample_data}")


    sanitized_data = sanitize_data(sample_data, config)

    logging.info(f"Sanitized Data: {sanitized_data}")


    # Simulate sending data to an external provider
    if "secret_field" not in sanitized_data : # Demonstrate the filtering in action.
        log_data_flow(sanitized_data, "external_provider", "prompt generation", config)  #Example - would be based on routing logic
    else:
        logging.warning("Secret field not filtered")

