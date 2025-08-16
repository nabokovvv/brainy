import logging
import re
from together import error
import matplotlib.pyplot as plt
import json
import os
import uuid
import config

logger = logging.getLogger(__name__)

async def get_chart_data_from_text(article_text: str, llm_client, model: str) -> str:
    """
    Asks the LLM to extract chart data from a given text using the provided client.
    """
    logger.info(f"--- Article Text Sent to LLM for Chart Extraction ---\n{article_text[:500]}...\n---------------------------------")
    
    prompt = f'''You are a fact extractor for visualization. Analyze the article text and return ONE chart as STRICT JSON. No explanations, no Markdown, no code — only valid JSON.

TASK
1) Read my research article and try to visualize some important findings.
2) Choose an appropriate chart type FROM THIS LIST: "bar", "line", "pie".
3) Return at least 3 data points.
4) Do not include units in the numbers (numbers only). Provide the unit separately in the "unit" field.
5) For a time axis, use ISO formats: YYYY, YYYY-MM, or YYYY-MM-DD.

IF THERE ARE FEWER THAN 3 DATA POINTS → return an error object following the error schema (see below). Otherwise return a chart object following the chart schema.

STRICT OUTPUT SCHEMA (return exactly one of the two objects):

1) chart schema (successful result):
{{
  "chart_type": "bar" | "line" | "pie",
  "title": "string",                 // short chart title
  "x_label": "string",               // X-axis label (for pie use "category")
  "y_label": "string",               // Y-axis label (for pie use "value" or "percent")
  "unit": "string|null",             // measurement unit, e.g. "%", "units", "₽"; null if none
  "data": [                          // at least 3 items
    {{ "x": "string", "y": number }}
  ]
}}

Constraints:
- "data" ≥ 3;
- "y" is a number only (decimal point ".", no text);
- "x" is a string (ISO date or category name);
- Additional fields are PROHIBITED (no other keys).

2) error schema (if insufficient data for ≥ 3 points):
{{
  "error": "insufficient_data",
  "reason": "string"   // brief reason why the chart cannot be built
}}

ARTICLE TEXT:
{article_text}

ANSWER WITH EXACTLY ONE JSON OBJECT FOLLOWING ONE OF THE TWO SCHEMAS. NO ADDITIONAL TEXT.
'''
    
    logger.info(f"Prompting model {model} for chart data...")
    try:
        # This assumes the client has a compatible `chat.completions.create` method
        response = await llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1024 
        )
        response_text = response.choices[0].message.content.strip()
        
        cleaned_text = re.sub(r'<think>.*?</think>', '', response_text, flags=re.S).strip()
        
        json_match = re.search(r'\{.*\}', cleaned_text, re.S)
        if json_match:
            return json_match.group(0)
        else:
            logger.warning(f"No JSON object found in cleaned response from chart generator: {cleaned_text}")
            return ""
            
    except error.APIError as e:
        logger.error(f"Chart Gen API Error: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred during chart generation API call: {e}", exc_info=True)
    return ""

def draw_chart(chart_data: dict, output_dir: str) -> tuple[str, str] | None:
    """
    Draws a chart based on the provided data and saves it as a PNG file.
    Returns the file path and title, or None on failure.
    """
    try:
        chart_type = chart_data.get("chart_type")
        title = chart_data.get("title", "Chart")
        x_label = chart_data.get("x_label", "")
        y_label = chart_data.get("y_label", "")
        unit = chart_data.get("unit")
        data = chart_data.get("data")

        if not all([chart_type, data]):
            logger.warning("Chart data is missing required fields.")
            return None

        if len(data) < 3:
            logger.warning("Insufficient data points to draw chart.")
            return None

        x_values = [item['x'] for item in data]
        y_values = [item['y'] for item in data]

        plt.figure(figsize=(10, 6))

        if chart_type == "bar":
            plt.bar(x_values, y_values)
        elif chart_type == "line":
            plt.plot(x_values, y_values, marker='o')
        elif chart_type == "pie":
            plt.pie(y_values, labels=x_values, autopct='%1.1f%%', startangle=90)
            plt.axis('equal')
        else:
            logger.warning(f"Unsupported chart type: {chart_type}")
            return None

        plt.title(title)
        
        if chart_type != "pie":
            plt.xlabel(x_label)
            y_axis_label = f"{y_label}{f' ({unit})' if unit else ''}"
            plt.ylabel(y_axis_label)
            plt.xticks(rotation=45, ha='right')

        plt.tight_layout()

        os.makedirs(output_dir, exist_ok=True)
        
        filename = f"{str(uuid.uuid4())[:16]}.png"
        filepath = os.path.join(output_dir, filename)
        
        plt.savefig(filepath)
        plt.close()
        
        logger.info(f"Chart saved to {filepath}")
        return filepath, title

    except Exception as e:
        logger.error(f"Failed to draw chart: {e}", exc_info=True)
        return None

async def generate_chart(article_text: str, llm_client, output_dir: str) -> tuple[str, str] | None:
    """
    Generates chart data from text, draws the chart, and saves it.
    Returns the file path and title of the chart image if successful.
    """
    chart_json_str = await get_chart_data_from_text(article_text, llm_client, config.TOGETHER_DEEPSEEK)
    
    if not chart_json_str:
        logger.info("LLM did not return JSON data for chart.")
        return None
        
    try:
        chart_data = json.loads(chart_json_str)
    except json.JSONDecodeError:
        logger.warning(f"Failed to decode JSON from chart generator: {chart_json_str}")
        return None

    if "error" in chart_data:
        logger.info(f"Chart generation skipped: {chart_data.get('reason')}")
        return None

    return draw_chart(chart_data, output_dir)