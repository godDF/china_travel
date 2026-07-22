def init_agent(kwargs):
    """Initialize only the requested agent and its dependencies.

    Importing every agent implementation eagerly makes the Web LLMNeSy path
    depend on optional packages used only by evaluation or other agents.
    """
    lang = kwargs.get("lang", "zh")
    method = kwargs["method"]

    if method == "RuleNeSy":
        from .nesy_agent.rule_driven_rec import RuleDrivenAgent

        return RuleDrivenAgent(
            env=kwargs["env"],
            backbone_llm=kwargs["backbone_llm"],
            cache_dir=kwargs["cache_dir"],
            debug=kwargs["debug"],
        )

    if method == "LLMNeSy":
        from .nesy_agent.llm_driven_rec import LLMDrivenAgent

        return LLMDrivenAgent(**kwargs)

    if method in {"Act", "ReAct", "ReAct0"}:
        from .pure_neuro_agent.pure_neuro_agent import ActAgent, ReActAgent
        from .pure_neuro_agent.prompts import (
            ZEROSHOT_ACT_INSTRUCTION,
            ZEROSHOT_REACT_INSTRUCTION,
            ZEROSHOT_REACT_INSTRUCTION_GLM4,
            ONESHOT_REACT_INSTRUCTION,
            ONESHOT_REACT_INSTRUCTION_GLM4,
        )

        if lang == "en":
            from .pure_neuro_agent.prompts.prompts_en import (
                ONESHOT_REACT_INSTRUCTION as EN_ONESHOT_REACT_INSTRUCTION,
            )
            ONESHOT_REACT_INSTRUCTION = EN_ONESHOT_REACT_INSTRUCTION

        if method == "Act":
            return ActAgent(
                env=kwargs["env"],
                backbone_llm=kwargs["backbone_llm"],
                prompt=ZEROSHOT_ACT_INSTRUCTION,
            )

        use_glm_prompt = "glm4" in kwargs["backbone_llm"].name.lower()
        if method == "ReAct":
            prompt = (
                ONESHOT_REACT_INSTRUCTION_GLM4
                if use_glm_prompt
                else ONESHOT_REACT_INSTRUCTION
            )
        else:
            prompt = (
                ZEROSHOT_REACT_INSTRUCTION_GLM4
                if use_glm_prompt
                else ZEROSHOT_REACT_INSTRUCTION
            )
        return ReActAgent(
            env=kwargs["env"],
            backbone_llm=kwargs["backbone_llm"],
            prompt=prompt,
        )

    if method == "LLM-modulo":
        from .nesy_verifier import LLMModuloAgent

        modulo_kwargs = dict(kwargs)
        modulo_kwargs["model"] = modulo_kwargs["backbone_llm"]
        modulo_kwargs["max_steps"] = modulo_kwargs["refine_steps"]
        return LLMModuloAgent(**modulo_kwargs)

    if method == "TPCAgent":
        from .tpc_agent.tpc_agent import TPCAgent

        return TPCAgent(**kwargs)

    if method == "UrbanTrip":
        from .UrbanTrip.tpc_agent import UrbanTrip

        return UrbanTrip(**kwargs)

    raise ValueError(f"Unsupported agent method: {method}")


def init_llm(llm_name, max_model_len=None):
    from .llms import Deepseek, GPT4o, GLM4Plus, Qwen, Mistral, Llama, EmptyLLM

    from .tpc_agent.tpc_llm import TPCLLM

    if llm_name == "deepseek":
        llm = Deepseek()
    elif llm_name == "gpt-4o":
        llm = GPT4o()
    elif llm_name == "glm4-plus":
        llm = GLM4Plus()
    elif "Qwen" in llm_name:
        llm = Qwen(llm_name, max_model_len=max_model_len)
    elif llm_name == "mistral":
        llm = Mistral(max_model_len=max_model_len)
    elif "Llama" in llm_name:
        llm = Llama(llm_name)
    elif llm_name == "rule":
        return EmptyLLM()
    elif llm_name == "TPCLLM":
        llm = TPCLLM()
    else:
        raise Exception("Not Implemented")

    return llm
