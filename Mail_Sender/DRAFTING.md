# Drafting failure handling

Every run first submits a synthetic image and requires correct JSON identifying its color. Listing a model is not sufficient. `python Mail_Sender/draft_cloud.py --check-only` performs only this check. The workflow's check_only input runs the same path using GitHub secrets and does not draft or change records.

Generation reserves 4096 output tokens because some providers count reasoning within the same budget. A length-truncated response is rejected and retried with up to 8192 tokens. Output-validation errors include corrective feedback and are separate from transport/provider outages, so they cannot trip the provider circuit. With check_only=true, preview_appid tests that game's real R2 sprite sheet and template without saving a draft or changing its database state.

MAIL_WRITER_BASE_URL, MAIL_WRITER_MODEL and MAIL_WRITER_API_KEY take precedence. Existing TRIAGE_* settings remain a local compatibility fallback. The SDK has automatic retries disabled. Each inference request uses a 60-second timeout and at most three attempts. Unsupported-image routes, inactive provider accounts and HTTP 400/401/403/404/422 stop immediately. Other inference/output failures open the batch circuit after three consecutively failed leads. No alternate model is selected automatically.

The queue stops before the 350-minute workflow ceiling with a 330-minute processing budget. Failures remain Writing. Missing media is reported separately and is not classified as a provider outage. Completed manifests are preserved for recovery. A run with skipped or failed leads returns nonzero rather than reporting success. The final summary reports completed/recovered, skipped and initial queue sizes.

An OmniRoute endpoint that routes images to text-only backends, returns unavailable model routes, or has no connected provider accounts must be repaired on the provider side. The application cannot make those routes support vision. This implementation prevents repeated queue-wide failures and validates capability before drafting.
