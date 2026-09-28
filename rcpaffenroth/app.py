import gradio as gr
from huggingface_hub import InferenceClient
from transformers import pipeline
from huggingface_hub import HfApi, InferenceClient

LOCAL_MODEL = "Qwen/Qwen3-0.6B"
REMOTE_MODEL = "openai/gpt-oss-20b"


pipe = pipeline(
    "text-generation",
    model=LOCAL_MODEL,
    dtype="auto",
    device="cpu",
)


fancy_css = """
.gradio-container {
    width: 96% !important;
    max-width: none !important;
}
#app-title {
    text-align: center;
    margin-bottom: 4px;
}
#app-subtitle {
    text-align: center;
    color: var(--body-text-color-subdued);
    margin-bottom: 24px;
}
#chat-container {
    width: 100%;
    border: 1px solid var(--border-color-primary);
    border-radius: 12px;
    padding: 16px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}
#model-note {
    font-size: 0.9em;
    color: var(--body-text-color-subdued);
    margin-top: 8px;
}
@media (max-width: 768px) {
    .gradio-container {
        width: 98% !important;
    }
    #chat-container {
        padding: 8px;
    }
}
"""


# Removed @spaces.GPU
def local_generate(
    messages,
    max_tokens,
    temperature,
    top_p,
):
    outputs = pipe(
        messages,
        max_new_tokens=max_tokens,
        do_sample=True,
        temperature=temperature,
        top_p=top_p,
    )

    return outputs[0]["generated_text"][-1]["content"]


def respond(
    message,
    history: list[dict[str, str]],
    system_message,
    max_tokens,
    temperature,
    top_p,
    use_local_model,
    hf_token,  # Now an ordinary string
):
    messages = [{"role": "system", "content": system_message}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})

    if use_local_model:
        print("[MODE] local")

        response = local_generate(
            messages,
            max_tokens,
            temperature,
            top_p,
        )

        yield response
        return

    print("[MODE] api")

    if not hf_token:
        yield "⚠️ Please enter your Hugging Face token first."
        return

    client = InferenceClient(
        token=hf_token,
        model=REMOTE_MODEL,
    )

    response = ""

    for chunk in client.chat_completion(
        messages,
        max_tokens=max_tokens,
        stream=True,
        temperature=temperature,
        top_p=top_p,
    ):
        choices = chunk.choices
        token = ""

        if len(choices) and choices[0].delta.content:
            token = choices[0].delta.content

        response += token
        yield response


chatbot = gr.ChatInterface(
    fn=respond,
    additional_inputs=[
        gr.Textbox(
            value="You are a friendly Chatbot.",
            label="System message",
        ),
        gr.Slider(
            minimum=1,
            maximum=2048,
            value=512,
            step=1,
            label="Max new tokens",
        ),
        gr.Slider(
            minimum=0.1,
            maximum=2.0,
            value=0.7,
            step=0.1,
            label="Temperature",
        ),
        gr.Slider(
            minimum=0.1,
            maximum=1.0,
            value=0.95,
            step=0.05,
            label="Top-p (nucleus sampling)",
        ),
        gr.Checkbox(
            label="Use Local Model",
            value=False,
        ),
        # Replaces gr.LoginButton and gr.OAuthToken
        gr.Textbox(
            label="Hugging Face Token",
            placeholder="hf_...",
            type="password",
        ),
    ],
)

def validate_hf_token(hf_token):
    if not hf_token or not hf_token.strip():
        return "⚠️ Enter a Hugging Face token."

    try:
        account = HfApi(token=hf_token.strip()).whoami()
        username = account.get("name", "unknown user")
        return f"Valid Hugging Face token for **{username}**."
    except Exception:
        return (
            "The token could not be validated. "
        )

with gr.Blocks(css=fancy_css) as demo:
    gr.Markdown(
        "# 🌟 Effective AI Chatbot",
        elem_id="app-title",
    )

    gr.Markdown(
        "A fancier version of the standard Hugging Face chatbot template.",
        elem_id="app-subtitle",
    )

    # Token entry immediately below the header
    with gr.Row():
        hf_token = gr.Textbox(
            label="Hugging Face Token",
            placeholder="hf_...",
            type="password",
            scale=4,
        )

        validate_button = gr.Button(
            "Validate Token",
            scale=1,
        )

    token_status = gr.Markdown()

    with gr.Accordion("Additional inputs", open=False):
        system_message = gr.Textbox(
            value="You are a friendly Chatbot.",
            label="System message",
        )

        max_tokens = gr.Slider(
            minimum=1,
            maximum=2048,
            value=512,
            step=1,
            label="Max new tokens",
        )

        temperature = gr.Slider(
            minimum=0.1,
            maximum=2.0,
            value=0.7,
            step=0.1,
            label="Temperature",
        )

        top_p = gr.Slider(
            minimum=0.1,
            maximum=1.0,
            value=0.95,
            step=0.05,
            label="Top-p (nucleus sampling)",
        )

        use_local_model = gr.Checkbox(
            label="Use Local Model",
            value=False,
        )

    with gr.Column(elem_id="chat-container"):
        chatbot_component = gr.Chatbot()

        gr.ChatInterface(
            fn=respond,
            chatbot=chatbot_component,
            additional_inputs=[
                system_message,
                max_tokens,
                temperature,
                top_p,
                use_local_model,
                hf_token,
            ],
        )

        gr.Markdown(
            "Use **Additional inputs** to switch between the API model "
            "and the locally executed model.",
            elem_id="model-note",
        )

    validate_button.click(
        fn=validate_hf_token,
        inputs=hf_token,
        outputs=token_status,
        api_visibility="private",
    )


if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
    )
