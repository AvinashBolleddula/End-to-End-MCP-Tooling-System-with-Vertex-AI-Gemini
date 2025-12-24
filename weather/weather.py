# Used for type hints (Any means "this value can be of any type")
from typing import Any

# Async HTTP client library, httpx installed using uv add MCP-httpx
# Used to call external APIs (here, the National Weather Service API)
import httpx

# Import FastMCP server class from MCP-server-fastmcp package, a high lever MCP server framework for building MCP servers easily
# Handles JSON-RPC requests and responses, Tools, Lifecycles, etc.
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
# The string "weather" is the name of this MCP server
# This object will register tools and handle incoming MCP requests
mcp = FastMCP("weather")


# Constants
# Base URL for the National Weather Service API
NWS_API_BASE = "https://api.weather.gov"
# Required HTTP header for NWS API requests
USER_AGENT = "weather-app/1.0"

# Function to make requests to the NWS API
# Fetches data from NSW API endpoints and returns JSON responses IF successful (else None)
async def make_nws_request(url: str) -> dict[str, Any] | None:
    """Make a request to the NWS API with proper error handling."""
    # Required headers for NWS API requests
    # User-Agent: NSW API requires a User-Agent header to identify the application making the request
    # Accept: application/geo+json indicates we want the response in GeoJSON format
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    # Creates an asynchronous HTTP client session
    # Non -blocking, allowing other operations to run while waiting for the response
    # Automatically closes the client session after the request is complete
    async with httpx.AsyncClient() as client:
        try:
            # Make GET request to the specified URL with headers and a timeout of 30 seconds
            response = await client.get(url, headers=headers, timeout=30.0)
            # Raise an exception for HTTP error responses (4xx and 5xx status codes)
            response.raise_for_status()
            # If the request is successful, return the JSON response
            return response.json()
        except Exception:
            # If any error occurs (network issues, invalid responses, etc.), return None
            # Fails safely without crashing the application
            return None


# Converts raw alert data from NWS API into a human-readable format
def format_alert(feature: dict) -> str:
    """Format an alert feature into a readable string."""
    # Extract properties from the alert feature
    props = feature["properties"]
    # Return a formatted string with alert details
    # Uses get() method to safely access dictionary keys with default values
    return f"""
Event: {props.get("event", "Unknown")}
Area: {props.get("areaDesc", "Unknown")}
Severity: {props.get("severity", "Unknown")}
Description: {props.get("description", "No description available")}
Instructions: {props.get("instruction", "No specific instructions provided")}
"""

# @mcp.tool decorator registers the function as a mcp tool in the MCP server
# exposes to clients/llm
# inputs and outputs are automatically converted to mcp json-rpc schema by the MCP framework
@mcp.tool()
# Tool to fetch active weather alerts for a given US state
async def get_alerts(state: str) -> str:
    """Get weather alerts for a US state.

    Args:
        state: Two-letter US state code (e.g. CA, NY)
    """
    # Builds the NWS alerts endpoint
    url = f"{NWS_API_BASE}/alerts/active/area/{state}"
    
    # Calls the NWS API safely using the helper function
    data = await make_nws_request(url)

    # Handles API Failure or invalid response
    if not data or "features" not in data:
        return "Unable to fetch alerts or no alerts found."

    # No active alerts case
    if not data["features"]:
        return "No active alerts for this state."
    
    # converts raw alert json data into human-readable format
    alerts = [format_alert(feature) for feature in data["features"]]
    
    # returns all aerts as a single formatted string separated by "---"
    return "\n---\n".join(alerts)




@mcp.tool()
# Tool to fetch weather forecast for a given latitude and longitude
async def get_forecast(latitude: float, longitude: float) -> str:
    """Get weather forecast for a location.

    Args:
        latitude: Latitude of the location
        longitude: Longitude of the location
    """
    # First get the forecast grid endpoint
    points_url = f"{NWS_API_BASE}/points/{latitude},{longitude}"
    # Gets Metadata about the location, including forecast URLs
    points_data = await make_nws_request(points_url)

    if not points_data:
        return "Unable to fetch forecast data for this location."

    # Get the forecast URL from the points response
    forecast_url = points_data["properties"]["forecast"]
    
    # Fetch the detailed forecast data
    forecast_data = await make_nws_request(forecast_url)

    if not forecast_data:
        return "Unable to fetch detailed forecast."

    # Format the periods into a readable forecast
    # Forecast broken down into time periods (e.g., "Tonight", "Monday", etc.)
    periods = forecast_data["properties"]["periods"]
    forecasts = []
    # Only show next 5 periods
    for period in periods[:5]:  
        forecast = f"""
{period["name"]}:
Temperature: {period["temperature"]}°{period["temperatureUnit"]}
Wind: {period["windSpeed"]} {period["windDirection"]}
Forecast: {period["detailedForecast"]}
"""
        forecasts.append(forecast)

    # return all forecasts as a single formatted string separated by "---"
    return "\n---\n".join(forecasts)

# Entry point to run the MCP server
def main():
    # Initialize and run the server using standard input/output for communication
    # server communicates with clients via stdio
    # designed for local tools, clis and mcp clients
    # json rpc requests and responses are sent over stdio streams
    # when using stdio never print to stdout directly as it will interfere with the mcp protocol
    mcp.run(transport="stdio")


# ensure main() is called when this script is executed directly
# prevents main() from being called if this script is imported as a module
if __name__ == "__main__":
    main()