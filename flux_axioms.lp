#program base.

% Safety constraint for MODIFY_SELF
:- self_modify(A), not sandboxed(A).
:- self_modify(A), not approved(A, akhil).

% Safety constraint for EXECUTE
:- execute(A), not sandboxed(A), not approved(A, akhil).

% Safety constraint for WRITE
:- write(F, SYSTEM), not approved(F, akhil).

% Safety constraint for DELETE
:- delete(F), system_file(F), not approved(F, akhil).

% Safety constraint for ATTACK
:- attack(T), not approved(T, akhil).

% Safety constraint for AUDIT
:- approved(A, akhil), not audit_verified(A, akhil).

% Safety constraint for UPDATE_BELIEF
:- update_belief(C, S1, S2), not evidence_sufficient(C, S2).

% Safety constraint for ROLLBACK
:- rollback(V), not checkpoint_exists(V).

% Safety constraint for SPAWN_AGENT
:- spawn_agent(A, P), P > trust_level(flux).

% Safety constraint for PIPELINE
:- pipeline(P), member(A, P), requires_approval(A), not approved(P, akhil).

% Safety constraint for RESUME
:- resume(A), not suspended(A).

% Identity constraint for AKHIL
:- approved(A, akhil), not audit_verified(A, akhil).
:- operator(X), X != akhil.

% Identity constraint for USER
:- user_command(C), not sandboxed(C).
:- user_command(C), modifies_state(C).
