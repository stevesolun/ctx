---
version: alpha
name: Dispatch Mono
description: Independent-press monospace. Bulletin-board energy.
colors:
  primary: "#1B1714"
  secondary: "#716A62"
  tertiary: "#D9541A"
  neutral: "#F6F1E7"
  surface: "#FDF9EF"
  on-primary: "#FDF9EF"
typography:
  display:
    fontFamily: IBM Plex Mono
    fontSize: 3.25rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: IBM Plex Mono
    fontSize: 1.8rem
    fontWeight: 600
  body:
    fontFamily: IBM Plex Mono
    fontSize: 0.94rem
    lineHeight: 1.65
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.72rem
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

A newsletter-press system: monospace body, one rubber-stamp orange.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1B1714`):** Headlines and core text.
- **Secondary (`#716A62`):** Borders, captions, and metadata.
- **Tertiary (`#D9541A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F6F1E7`):** The page foundation.

## Typography

- **display:** IBM Plex Mono 3.25rem
- **h1:** IBM Plex Mono 1.8rem
- **body:** IBM Plex Mono 0.94rem
- **label:** IBM Plex Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
