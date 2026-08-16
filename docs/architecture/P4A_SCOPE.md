# P4A Scope Freeze

P4A is limited to architecture, inventory, shared semantics, recovery rules and CI hard gates for external business effects.

P4A does **not**:

- enable a search provider;
- enable reminder, birthday or digest delivery;
- enable lifecycle notification delivery;
- execute real lifecycle refunds;
- add provider credentials;
- alter P3E runtime identities;
- change RLS policies;
- add BYPASSRLS;
- widen worker table privileges;
- change the certified P3E migrations.

Provider implementation begins only after P4A same-head certification, starting with P4B search integration.
