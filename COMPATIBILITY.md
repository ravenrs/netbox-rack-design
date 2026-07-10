# Compatibility

This document tracks the minimum and maximum supported NetBox versions for each release of NetBox Rack Design.

| Plugin Version | Minimum NetBox Version | Maximum NetBox Version |
|----------------|------------------------|------------------------|
| 0.10.0 | 4.4.0 | 4.4.99 |
| 0.9.1 | 4.4.0 | 4.4.99 |
| 0.9.0 | 4.4.0 | 4.4.99 |
| 0.8.0 | 4.4.0 | 4.4.99 |
| 0.7.0 | 4.4.0 | 4.4.99 |
| 0.6.0 | 4.4.0 | 4.4.99 |
| 0.5.0 | 4.4.0 | 4.4.99 |
| 0.4.0 | 4.4.0 | 4.4.99 |
| 0.3.0 | 4.4.0 | 4.4.99 |
| 0.2.0 | 4.4.0 | 4.4.99 |
| 0.1.0 | 4.4.0 | 4.4.99 |

## Notes

- This plugin requires Python 3.12 or later
- Always test your plugin with the target NetBox version before upgrading in production
- Check the [NetBox release notes](https://docs.netbox.dev/en/stable/release-notes/) for breaking changes

## Upgrading

When upgrading NetBox or this plugin:

1. Review the NetBox release notes for any breaking changes
2. Test the upgrade in a development environment
3. Backup your database before upgrading production
4. Run database migrations: `python manage.py migrate`
5. Clear the cache: `python manage.py clearcache`
