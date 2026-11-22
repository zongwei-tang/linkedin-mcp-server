---
name: linkedin-mcp
description: Use LinkedIn data for profile, company, job, post, feed, inbox, recruiting, and outreach work through the bundled LinkedIn MCP server. Use only for an explicit LinkedIn request.
---

# LinkedIn MCP

Use the bundled `linkedin` MCP server only when the user explicitly asks for
LinkedIn data or an action on LinkedIn. Do not call LinkedIn tools for unrelated
work, health checks, or background maintenance.

## Operating rules

- Start with the smallest read operation that can answer the request.
- Treat profile, company, job, post, feed, and message results as live LinkedIn
  evidence. Distinguish retrieved facts from inference.
- Keep searches and result pages modest. Do not collect data in bulk or spam.
- Never enable the plugin or its MCP server, edit the host's plugin configuration,
  or start a login flow merely because LinkedIn might be useful. If either
  component is disabled, explain that state and stop.
- `send_message`, `reply_to_conversation` and `connect_with_person` are write
  actions. Use them only when the user explicitly authorizes the
  exact recipient and action. Confirm the final message or connection note
  unless the user has already supplied it.
- `archive_conversation` is a write action too: it moves a thread out of the
  user's inbox. Use it only when the user explicitly asks to archive that
  thread.
- `star_conversation`, `unstar_conversation`, `mute_conversation` and
  `unmute_conversation` change only the user's own view of a thread, and
  opening the thread marks it read. Use them for the thread the user named.
- `like_post`, `react_to_post` and `unlike_post` are write actions others can see. Use them only
  on a post the user named.
- `save_post` and `unsave_post` change only the user's own saved posts (the
  author is not told). Use them for the post the user named.
- `follow_person`, `unfollow_person`, `follow_company` and `unfollow_company`
  change whose posts reach the user's feed. Use them only for the person or
  page the user named.
- `ignore_invitation` removes a pending invitation for good. Use it only for
  the sender the user named.
- `save_job` and `unsave_job` change the user's saved jobs, which only the user
  sees. Use them only for postings the user named.
- `dismiss_job` hides a job from every later search and cannot be undone. Use
  it only for jobs the user asked to dismiss.
- Do not retry a failed write action unless the result proves it was not sent.

## Browser and authentication

The server uses a managed Chromium browser and the user's own LinkedIn session.
Installing or enabling the plugin does not sign in. The first LinkedIn data
request may prepare the browser, import an existing local session, or require a
visible login window. If LinkedIn presents a captcha, two-factor prompt, or
other user-owned authentication step, stop and ask the user to complete it.

If browser setup or authentication is still in progress, report that exact
state and retry once after it finishes. Do not loop, launch extra browser
instances, clear profiles, or replace the user's browser session.

## Tool selection

- Profiles: `get_my_profile`, `get_person_profile`, `search_people`, and
  `get_sidebar_profiles`.
- Companies: `get_company_profile`, `get_company_posts`, `search_companies`,
  and `get_company_employees`.
- Jobs: `search_jobs`, `get_saved_jobs`, `get_job_details`,
  `get_job_apply_url` and `get_easy_apply_form`. `get_easy_apply_form` opens
  the Easy Apply form, so use it only when the user is preparing an
  application.
- Content: `get_feed`, `search_posts` and `get_post_details`, which reads one
  post and its comments from any `feed_post` reference the others return.
- Network: `get_invitations`, `get_connections`, `get_notifications`,
  `get_profile_views`, `get_followers`, `get_following`,
  `get_followed_pages`, `get_suggested_people`, `get_catch_up`,
  `get_my_groups`, `get_my_events`, `get_my_newsletters`,
  `get_group_details`, `get_event_details`, `search_groups` and
  `search_events`.
- Analytics, private to the user: `get_my_dashboard`,
  `get_search_appearances` and `get_content_analytics`.
- Schools and reading: `get_school_profile`, `get_school_alumni`,
  `search_schools`, `get_saved_posts`, `get_article` and `get_newsletter`.
- Messages: `get_inbox`, `get_conversation`, and `search_conversations`.
- Writes: `send_message`, `reply_to_conversation` (answer an existing thread,
  InMail included), `connect_with_person`, `archive_conversation`,
  `star_conversation`, `unstar_conversation`, `mute_conversation`,
  `unmute_conversation`, `mark_thread_unread`, `mark_thread_read`,
  `like_post`, `react_to_post`, `unlike_post`, `save_post`, `unsave_post`,
  `follow_person`,
  `unfollow_person`, `follow_company`, `unfollow_company`,
  `ignore_invitation`, `save_job`, `unsave_job` and `dismiss_job`, subject to
  the explicit authorization rules above.
- Session: `get_session_status` says whether a login is saved without opening
  a browser or a login window; it does not prove LinkedIn still accepts it.
- Cleanup: use `close_session` only when the user asks to end the managed
  browser session or when the current LinkedIn task is finished and no follow-up
  call is expected.
