from scoped_rbac.policy import policy_from_json
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

TYPE_URI_PARTNER_PROFILES = "tests://PartnerProfiles"
TYPE_URI_LIST_PARTNER_PROFILES = f"https://api.openteams.com/json-schema/ResourceCollection?contents_type={TYPE_URI_PARTNER_PROFILES}"  # noqa: E501

policy_for_unauthenticated = policy_from_json(
    {
        # def policy_for_unauthenticated():
        # raise Exception("policy_for_unauthenticated")
        # return policy.policy_from_json({
        DEFAULT_CONTEXT: {"http.POST": [TYPE_URI_PARTNER_PROFILES,],},  # noqa: E231
    }
)

policy_for_staff = policy_from_json(
    {
        DEFAULT_CONTEXT: {
            "http.GET": [TYPE_URI_PARTNER_PROFILES, TYPE_URI_LIST_PARTNER_PROFILES],
            "http.POST": [TYPE_URI_PARTNER_PROFILES],
            "http.PUT": [TYPE_URI_PARTNER_PROFILES],
            "http.DELETE": [TYPE_URI_PARTNER_PROFILES],
        },
    }
)
