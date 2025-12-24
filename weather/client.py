# Needed for async/await execution (MCP + LLM calls are async)
import asyncio

import os

# Used for type hints (Optional[ClientSession])
from typing import Optional

# Safely manages multiple async resources
# Ensures MCP session, stdio connection, etc are cleanly closed on exit
from contextlib import AsyncExitStack

# MCP client-side components
# stdio_client: connects to MCP server over stdio (stdin/stdout)
# ClientSession: manages tools discovery, tool calls, LLM calls, etc
# StdioServerParameters: how to start the MCP server process
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


from google import genai
from google.genai import types

# loads environment variables from a .env file
# project id and location for vertex ai LLM calls
from dotenv import load_dotenv
load_dotenv()  


# wrapper class for LLM + MCP session lifecycle
class MCPClient:
    def __init__(self):
        # will hold the active mcp session once connected
        self.session: Optional[ClientSession] = None
        # manages cleanup of stdio connection, mcp session, backgrounf tasks etc
        self.exit_stack = AsyncExitStack()
        #
        self.project = os.environ['GOOGLE_CLOUD_PROJECT']
        self.location = os.environ['GOOGLE_CLOUD_LOCATION']
        self.gemini_model = os.environ['GEMINI_MODEL']

        self.genai_client = genai.Client(vertexai=True, project=self.project, location=self.location)
    
    # methods will go here
    # Async method to start and connect to an MCP server over stdio
    async def connect_to_server(self, server_script_path: str):
        """Connect to an MCP server

        Args:
            server_script_path: Path to the server script (.py or .js)
        """
        # Detects whether the server is a Python or Node.js script
        is_python = server_script_path.endswith('.py')
        is_js = server_script_path.endswith('.js')
        # mcp stdio server must be either a python or node.js script
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")
        
        # choose the correct runtime command based on script type
        command = "python" if is_python else "node"
        
        # Define how to start the MCP server process
        # command: "python" or "node"
        # args: [path to server script]
        # No special env vars needed here, so env=None
        server_params = StdioServerParameters(
            command=command,
            args=[server_script_path],
            env=None
        )
        
        # starts the server and opens stdin/stdout pipes
        stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
        
        # stores read/write streams for json-rpc communication
        self.stdio, self.write = stdio_transport
        
        # creates an MCP client session over the stdio connection
        self.session = await self.exit_stack.enter_async_context(ClientSession(self.stdio, self.write))

        # performs mcp handshake (capabilities exchange, tool discovery, protocol setup, etc)
        await self.session.initialize()

        # List available tools
        # ask server for its available tools
        response = await self.session.list_tools()
        tools = response.tools
        
        # confirms connection and prints available tools
        # this print is safe because this is the client, not the stdio server
        print("\nConnected to server with tools:", [tool.name for tool in tools])

    
    
    
    # Process a query using Vertex AI Gemini and MCP tools
    async def process_query(self, query: str) -> str:
        """Process a query using Vertex AI Gemini + MCP tools (loops until no more tool calls)."""
        if not self.session:
            raise RuntimeError("Not connected to an MCP server. Call connect_to_server() first.")

        # 1) Fetch MCP tools and convert them to Gemini FunctionDeclarations
        tools_resp = await self.session.list_tools()

        function_decls = []
        for t in tools_resp.tools:
            # MCP provides JSON schema in t.inputSchema -> Gemini expects parameters schema
            params_schema = t.inputSchema or {"type": "object", "properties": {}}
            if "type" not in params_schema:
                params_schema["type"] = "object"

            function_decls.append(
                types.FunctionDeclaration(
                    name=t.name,
                    description=t.description or "",
                    parameters=params_schema,
                )
            )

        gemini_tools = [types.Tool(function_declarations=function_decls)]
        config = types.GenerateContentConfig(tools=gemini_tools)

        # 2) Conversation state for Gemini
        contents = [
            types.Content(role="user", parts=[types.Part(text=query)])
        ]

        final_text_parts: list[str] = []

        # 3) Tool-call loop
        while True:
            resp = self.genai_client.models.generate_content(
                model=self.gemini_model,
                contents=contents,
                config=config,
            )

            # Gemini responses are in candidate content parts
            parts = (resp.candidates[0].content.parts or []) if resp.candidates else []

            # Collect any text
            for p in parts:
                if getattr(p, "text", None):
                    final_text_parts.append(p.text)

            # Extract function calls (tool requests)
            tool_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

            # If no tool calls -> we are done
            if not tool_calls:
                break

            # Add the model message that requested tools to the conversation
            contents.append(types.Content(role="model", parts=parts))

            # Execute each tool call and return function responses back to Gemini
            tool_response_parts = []
            for fc in tool_calls:
                tool_name = fc.name
                tool_args = fc.args or {}

                mcp_result = await self.session.call_tool(tool_name, tool_args)

                # MCP returns a structured "content" list; pass it back as JSON-like object
                tool_response_parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=tool_name,
                            response={"result": mcp_result.content},
                        )
                    )
                )
            # Add tool responses to the conversation and continue the loop
            contents.append(types.Content(role="user", parts=tool_response_parts))

        # Join and return the final text response if no more tool calls
        return "\n".join([t for t in final_text_parts if t.strip()])


    
    # chat_loop method
    # runs an interactive terminal chat with the MCP client
    async def chat_loop(self):
        """Run an interactive chat loop"""
        print("\nMCP Client Started!")
        print("Type your queries or 'quit' to exit.")

        # keep the chat loop running until user types 'quit'
        while True:
            try:
                # read user input from terminal
                query = input("\nQuery: ").strip()

                # exit condition
                if query.lower() == 'quit':
                    break

                # send query to process_query method
                # which handles LLM calls and tool executions
                response = await self.process_query(query)
                # print the final response to the terminal
                print("\n" + response)
            # catch and print any errors during processing
            except Exception as e:
                print(f"\nError: {str(e)}")
    
    # cleanup method
    async def cleanup(self):
        """Clean up resources"""
        # closes all async resources managed by exit_stack
        # including MCP session, stdio connection, background tasks, server process, etc
        await self.exit_stack.aclose()


# async entry point for the mcp client
async def main():
    # ensures the user pases the server script path as a command line argument
    if len(sys.argv) < 2:
        print("Usage: python client.py <path_to_server_script>")
        sys.exit(1)

    # creates the MCP client instance
    # manages connection, llm+tools, chat loop, cleanup, etc
    client = MCPClient()
    try:
        # starts the mcp server as a subprocess 
        # and connects to it via json-rpc over stdio
        # discovers available tools, performs handshake, etc
        await client.connect_to_server(sys.argv[1])
        
        # runs the interactive chat loop
        # reads user queries from terminal
        # runs process_query for each query, which handles LLM calls and tool executions
        # prints the final response to the terminal
        await client.chat_loop()
    finally:
        # ensures all resources are cleaned up on exit
        await client.cleanup()

if __name__ == "__main__":
    import sys
    asyncio.run(main())


