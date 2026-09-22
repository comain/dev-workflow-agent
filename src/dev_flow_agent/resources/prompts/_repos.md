{% if repos is defined and repos and repos|length > 1 %}
## The repositories in play

This change spans more than one repository. All of them are checked out, on
the same branch, and all of them are yours to read and change:

{% for repo in repos %}- `{{ repo.name }}` -- {{ repo.path }}{% if repo.primary %} (the task's own){% endif %}
{% endfor %}
Work in whichever of them the change actually needs. Every commit covers all
of them at once, so a change that only makes sense together lands together.
{% endif %}
