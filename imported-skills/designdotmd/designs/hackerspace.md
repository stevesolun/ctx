---
version: alpha
name: Hackerspace
description: Solder room: PCB green, header amber, silkscreen white.
colors:
  primary: "#E9F0DB"
  secondary: "#87987D"
  tertiary: "#F2B53A"
  neutral: "#0B1810"
  surface: "#12241A"
  on-primary: "#0B1810"
typography:
  display:
    fontFamily: IBM Plex Mono
    fontSize: 3.5rem
    fontWeight: 700
  h1:
    fontFamily: IBM Plex Mono
    fontSize: 1.85rem
    fontWeight: 600
  body:
    fontFamily: IBM Plex Mono
    fontSize: 0.9rem
    lineHeight: 1.55
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.7rem
    letterSpacing: "0.08em"
rounded:
  sm: 0px
  md: 2px
  lg: 4px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A hackerspace-aesthetic palette: PCB green, solder-amber accent, silkscreen-white details.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E9F0DB`):** Headlines and core text.
- **Secondary (`#87987D`):** Borders, captions, and metadata.
- **Tertiary (`#F2B53A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0B1810`):** The page foundation.

## Typography

- **display:** IBM Plex Mono 3.5rem
- **h1:** IBM Plex Mono 1.85rem
- **body:** IBM Plex Mono 0.9rem
- **label:** IBM Plex Mono 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
