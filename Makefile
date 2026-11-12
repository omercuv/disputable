.PHONY: setup test demo demo-2 serve story story-2 eval
setup:   ; uv sync
test:    ; uv run pytest -q
demo:    ; uv run disputable demo
demo-2:  ; uv run disputable demo --ending 2
serve:   ; uv run disputable serve --approver Priya
story:   ; uv run disputable story --ending 1
story-2: ; uv run disputable story --ending 2
eval:    ; uv run disputable eval
