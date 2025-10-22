"""WiFi SSID and password generation service."""

import json
import random
import logging
from openai import AsyncOpenAI

from app.config import Config
from app.models import SSIDPasswordPair

logger = logging.getLogger(__name__)

# Initialize OpenAI client
openai_client = AsyncOpenAI(api_key=Config.OPENAI_API_KEY)


async def generate_ssid_password() -> SSIDPasswordPair:
    """Generate a memorable SSID and password pair using OpenAI.

    Returns:
        SSIDPasswordPair with generated credentials and hint

    Raises:
        HTTPException: If generation fails
    """
    try:
        prompt = """Generate a WiFi network SSID and password pair where:

1. The SSID should be creative, memorable, and somewhat quirky (like 'QuantumCoffeehouse' or 'NeonDreams42')
2. The password should be easy to guess/remember if you know the SSID, using a simple, consistent rule
3. The password should be reasonably secure (8+ characters)
4. Provide a hint that explains how to derive the password from the SSID

Examples of good patterns:
- SSID: "CosmicPizza88" → Password: "CP88!" (first letters + numbers + symbol)
- SSID: "NightOwlCafe" → Password: "nocafe" (first letters of each word + last word)
- SSID: "RetroWave2024" → Password: "retro2024" (first word + numbers)

Respond with ONLY a JSON object in this exact format:
{
  "ssid": "your_creative_ssid",
  "password": "derived_password",
  "hint": "explanation of how password relates to ssid"
}

Generate a new, unique combination now."""

        response = await openai_client.chat.completions.create(
            model=Config.OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are a creative WiFi network name generator. Generate memorable SSID/password pairs with simple derivation rules.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=200,
            temperature=0.9,  # High creativity
        )

        # Parse the JSON response
        content = response.choices[0].message.content.strip()

        # Clean up any markdown formatting
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            data = json.loads(content)
            return SSIDPasswordPair(
                ssid=data["ssid"],
                password=data["password"],
                hint=data.get("hint", "Derive password from SSID using the pattern"),
            )
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse OpenAI JSON response: {content}")
            # Fall back to fallback method
            return _generate_fallback_credentials()

    except Exception as e:
        logger.error(f"Error generating SSID/password with OpenAI: {e}")
        return _generate_fallback_credentials()


def _generate_fallback_credentials() -> SSIDPasswordPair:
    """Generate fallback credentials when OpenAI is unavailable.

    Returns:
        SSIDPasswordPair with simple fallback credentials
    """
    fallback_number = random.randint(1000, 9999)
    fallback_ssid = f"NetworkDown{fallback_number}"
    fallback_password = f"nd{fallback_number}"

    return SSIDPasswordPair(
        ssid=fallback_ssid,
        password=fallback_password,
        hint="Fallback mode: 'nd' + the number from SSID",
    )
